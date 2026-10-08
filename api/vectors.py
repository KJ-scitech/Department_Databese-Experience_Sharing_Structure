"""向量库：Chroma（落盘）+ sentence-transformers（生成向量）。

═══ 四个坑在这里落地 ═══
这些都是实测踩出来的，不是抄来的。改代码时别把它们改回去。

坑 1 · 距离空间必须是 cosine
    Chroma 不指定时用 l2，返回的是**平方**欧氏距离。
    于是「相似度 = 1 - distance」算出来的根本不是相似度
    （实测：真值 0.565 会被算成 0.130），而且**不报错**，静默给你错数字。

坑 2 · 不要给 query 加 instruction 前缀
    bge 模型主页建议检索任务加「为这个句子生成表示以用于检索相关文章：」，
    但在我们这种短句子上实测**五条结果相似度全部下降**。不加。

坑 3 · 集合名只能 ASCII
    Chroma 只允许 [a-zA-Z0-9._-]，写中文直接抛 InvalidArgumentError。
    所以集合叫 experience，中文只放文档内容里。

坑 4 · config 必须最先导入
    顺序**不能**调换，见下方 import 处的注释。
"""
import threading
from typing import Any

# ⚠️ 这一行必须排在 sentence_transformers 前面，不是排版问题。
#
# config 里会设置 HF_ENDPOINT（模型下载镜像）。而 huggingface_hub 在**被导入的那一刻**
# 就把 endpoint 读进模块常量了——之后再设环境变量也不生效。
# sentence_transformers 会连带把 huggingface_hub 导进来，所以只要它先跑，
# 镜像就白设了，模型下载会去连 hf.co（国内通常不通）。
#
# 以前能跑，纯粹是因为别的入口碰巧先导入了 api.db（→ 也就导入了 api.config），
# 把这个坑盖住了。哪天有人单独 import api.vectors，冷启动就会卡在下载上。
from . import config  # noqa: I001 —— 顺序是功能性的，不要合并排序

import chromadb
from sentence_transformers import SentenceTransformer

# ── 模块级缓存与锁 ─────────────────────────────────────────────
# 模型加载要 15–30 秒，每次请求都加载的话服务就没法用了。
# 模型和集合都用「双重检查锁」初始化：路由是同步函数，FastAPI 把它们丢进
# 线程池跑——裸的 `if _model is None` 在冷启动时可能被两个线程同时穿过，
# 各自加载一份模型（白吃 30 秒 + 双倍内存），Chroma 集合同理。
_model: SentenceTransformer | None = None
_collection: Any = None
_init_lock = threading.Lock()

# 发号用的锁和计数器，见 next_id() 的说明。
_id_lock = threading.Lock()
_id_counter = 0


def get_model() -> SentenceTransformer:
    """加载 embedding 模型（首次调用会下载，之后走本地缓存）。"""
    global _model
    if _model is None:
        with _init_lock:
            if _model is None:  # 双重检查：等锁期间可能已被别的线程加载好
                _model = SentenceTransformer(config.EMBEDDING_MODEL)
    return _model


def get_collection():
    """拿到 Chroma 集合（落盘，重启不丢）。"""
    global _collection
    if _collection is None:
        with _init_lock:
            if _collection is None:
                config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)

                # 用 PersistentClient 而不是 Client：
                # Client 是内存模式，进程一结束数据就没了——那是脚本的用法，不是服务的。
                client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))

                _collection = client.get_or_create_collection(
                    name=config.COLLECTION_NAME,          # 坑 3：ASCII 名
                    metadata={"hnsw:space": "cosine"},    # 坑 1：显式指定余弦空间
                )
    return _collection


def embedding_dim() -> int:
    """向量维度（bge-small-zh 是 512）。

    新版 sentence-transformers 把 get_sentence_embedding_dimension 改名成了
    get_embedding_dimension，旧名会发 FutureWarning。两个都试一下，谁有就用谁。
    """
    model = get_model()
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return getter()


def encode(texts: list[str]) -> list[list[float]]:
    """把文本转成向量。入库和查询共用这一个函数，保证两边编码方式一致。"""
    vectors = get_model().encode(texts, show_progress_bar=False)
    return [v.tolist() for v in vectors]


# ── 写 ────────────────────────────────────────────────────────

def add(exp_id: str, content: str, metadata: dict) -> None:
    """新增一条经验：正文进 documents，元信息进 metadata。

    id 已存在时 Chroma 会抛 ValueError → 路由层转 409/500。
    """
    collection = get_collection()
    collection.add(
        ids=[exp_id],
        documents=[content],
        embeddings=encode([content]),
        metadatas=[_clean_metadata(metadata)],
    )


def add_batch(items: list[dict]) -> int:
    """批量新增（seed / 导数据用），返回写入条数。

    逐条调 add() 的话，每条都要过一次模型——虽然模型已缓存，
    但 encode 的分词、推理开销乘以条数还是很可观；批量一次 encode
    能快一个数量级。空列表直接返回 0，Chroma 不接受空写入。

    每项形如 {"exp_id": ..., "content": ..., "metadata": {...}}。
    """
    if not items:
        return 0
    collection = get_collection()
    collection.add(
        ids=[item["exp_id"] for item in items],
        documents=[item["content"] for item in items],
        embeddings=encode([item["content"] for item in items]),
        metadatas=[_clean_metadata(item.get("metadata") or {}) for item in items],
    )
    return len(items)


def update(exp_id: str, content: str, metadata: dict) -> None:
    """修改一条经验（K8 补标签要走这里）。

    ⚠️ Chroma 的 update 对**不存在的 id 是静默无操作**，不报错。
    路由层拿它做 PATCH 时，会把「改成功」返回给一个根本不存在的条目。
    所以这里先查一次存在性，不存在就抛 KeyError → 路由转 404。

    （多一次 get 有点浪费，但 update 是低频操作，正确性优先。）
    """
    collection = get_collection()
    if not collection.get(ids=[exp_id])["ids"]:
        raise KeyError(f"经验不存在: {exp_id}")
    collection.update(
        ids=[exp_id],
        documents=[content],
        embeddings=encode([content]),
        metadatas=[_clean_metadata(metadata)],
    )


def update_tags(exp_id: str, tags: list[str]) -> dict:
    """只改标签、不动正文（K8 的 PATCH /api/experience/{exp_id} 用）。

    为什么单独给这个函数：PATCH 契约只开放 tags 字段。如果路由为了改个标签
    把整条记录读出来再原样写回，中间任何一个字段被并发改动都会被悄悄覆盖回去。
    这里用 Chroma update 只传 metadatas——**不传 documents/embeddings 时
    Chroma 不会重算向量**，正文零风险，也省一次模型推理。

    返回更新后的完整条目，路由直接拿去拼 ExperienceOut。
    """
    collection = get_collection()
    result = collection.get(ids=[exp_id])
    if not result["ids"]:
        raise KeyError(f"经验不存在: {exp_id}")
    collection.update(ids=[exp_id], metadatas=[_clean_metadata({"tags": tags})])
    return {
        "exp_id": exp_id,
        "content": result["documents"][0],
        "metadata": dict(collection.get(ids=[exp_id])["metadatas"][0] or {}),
    }


def delete(exp_id: str) -> None:
    """删除一条经验。id 不存在时 Chroma 静默成功——语义上「删没了」也算删成。"""
    get_collection().delete(ids=[exp_id])


# ── 读 ────────────────────────────────────────────────────────

def search(query: str, top_k: int = 5) -> list[dict]:
    """语义检索：输入一句话，返回最像的 top_k 条。

    注意用的是 query_embeddings 而不是 query_texts。
    用 query_texts 的话 Chroma 会拿它自带的 all-MiniLM-L6-v2（384 维）去编码，
    和我们入库用的 bge-small-zh（512 维）不是一个模型，维度对不上直接报错；
    就算维度碰巧一样，语义空间也不是同一个，结果没有意义。
    """
    if top_k < 1:
        return []

    collection = get_collection()
    total = collection.count()  # 只查一次，下面两处都要用
    if total == 0:
        return []

    # 坑 2：这里不加 instruction 前缀，和入库时的写法保持一致。
    query_vector = encode([query])[0]

    result = collection.query(
        query_embeddings=[query_vector],
        n_results=min(top_k, total),
    )

    items = []
    for exp_id, doc, meta, dist in zip(
        result["ids"][0],
        result["documents"][0],
        result["metadatas"][0],
        result["distances"][0],
    ):
        items.append(
            {
                "exp_id": exp_id,
                "content": doc,
                "metadata": dict(meta or {}),
                # 因为建集合时指定了 cosine，distance 就是 1 - 余弦相似度，
                # 所以 1 - distance 才是相似度。换个空间这个式子就不成立了。
                "similarity": round(1 - dist, 4),
            }
        )
    return items


def keyword_search(term: str, limit: int = 5) -> list[dict]:
    """按字面包含查找——**不是**向量检索，专门用来做对照。

    为什么要有这个函数：语义检索的分数再低也不是 0，
    光看分数看不出「向量检索和关键词检索到底差在哪」。
    这里用 Chroma 的字面过滤（相当于 SQL 的 LIKE %term%），
    问「设备出故障了咋办」时它一条都找不到，
    而「投影仪不亮」那条明明就在库里——差距一眼可见。

    两个特性要知道：
    - 大小写敏感（Chroma 的 $contains 是精确子串匹配）；
    - 空字符串会匹配到所有条目（等于不限条件），所以空词直接返回空列表。
    """
    if not term:
        return []
    result = get_collection().get(where_document={"$contains": term}, limit=limit)
    return [
        {"exp_id": i, "content": d, "metadata": dict(m or {})}
        for i, d, m in zip(result["ids"], result["documents"], result["metadatas"])
    ]


def get(exp_id: str) -> dict | None:
    """按 id 取单条经验的元信息（K3）。"""
    result = get_collection().get(ids=[exp_id])
    if not result["ids"]:
        return None
    return {
        "exp_id": result["ids"][0],
        "content": result["documents"][0],
        "metadata": dict(result["metadatas"][0] or {}),
    }


def id_num(exp_id: str) -> int:
    """取 exp-12 里的 12。取不出来返回 0。

    为什么要有这个小函数：id 是字符串，直接按字符串比大小，
    "exp-10" 会排在 "exp-2" **前面**（逐字符比 '1' < '2'）。
    经验列表分页就靠这个顺序，一旦用字符串序，第 1 页会是
    exp-1, exp-10, exp-11, exp-12, exp-2 …… 越翻越乱，
    而且**不报错**——只是顺序看起来有点怪，很容易被当成「就那样」放过去。

    只增不减的发号方式保证了编号连续，所以数字序＝录入顺序。
    """
    try:
        return int(exp_id.split("-")[1])
    except (IndexError, ValueError):
        return 0


def list_all(limit: int = 100, offset: int = 0) -> list[dict]:
    """列出经验（Chroma 没有分页，这里取全量后切片）。

    include 只取 documents 和 metadatas——默认还会带 embeddings（每条 512 个
    float），几百条经验就是几十 MB 的无用拷贝，纯粹浪费内存和时间。
    """
    result = get_collection().get(include=["documents", "metadatas"])
    items = [
        {
            "exp_id": i,
            "content": d,
            "metadata": dict(m or {}),
        }
        for i, d, m in zip(result["ids"], result["documents"], result["metadatas"])
    ]
    items.sort(key=lambda x: id_num(x["exp_id"]))  # 数字序，不是字符串序
    return items[offset : offset + limit]


def count() -> int:
    return get_collection().count()


def next_id() -> str:
    """生成下一个经验 id，形如 exp-9。

    ⚠️ 这里是「先读后写」，天生有竞态：两个请求几乎同时进来，
       各扫一遍现有 id，会算出**同一个** exp-N；后写的那个撞上
       Chroma 的重复 id，抛 ValueError → HTTP 500。

       路由是同步函数，FastAPI 把它们丢进线程池跑，所以同一个进程里
       真会并发。做法是加一把进程内的锁，并且让计数器**只增不减**：

         - 锁：把「扫现有 id + 自增」变成一个原子操作，同一刻只有一个线程在做。
         - 只增不减：中间就算有别的路径写进了 exp-9（比如 seed），
           我们算出来的是 max(计数器, 扫到的最大值) + 1，不会退回去重复发号。

       为什么不用「撞了再重试」：id 是在路由里先算好、再传进 add() 的。
       add() 里重试出来的新 id 传不回路由，响应和 participation.exp_ids
       都会指向一条不存在的经验。防撞比撞了再补更简单也更能保证正确。

       注：这只防**同一个进程内**的并发。跨进程同时开两个 Chroma
       PersistentClient 本来就不受支持（见 cli/client.py 开头的说明），
       架构上已经规定了服务是唯一写向量库的进程。

    性能说明：扫描用的是 get(include=[])，只要 id、不拉正文和 metadata。
    库到几百条时，全量拉 documents 是纯浪费——这个函数每次录入都要跑。
    """
    global _id_counter
    with _id_lock:
        used = max((id_num(i) for i in get_collection().get(include=[])["ids"]), default=0)
        _id_counter = max(_id_counter, used) + 1
        return f"exp-{_id_counter}"


def clear() -> None:
    """清空集合（seed 重建时用）。

    只该在服务启动前（scripts/seed）调用。运行中的请求如果握着旧集合对象，
    删除集合后它们会拿到 Chroma 的内部错误——这是 seed 的语义，不是服务的。
    """
    global _collection, _id_counter
    with _init_lock:
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        try:
            client.delete_collection(config.COLLECTION_NAME)
        except Exception:
            pass
        _collection = None
        # 集合没了，号也得从头开始，否则 seed 造出来的第一条会是 exp-9 而不是 exp-1。
        # （不清零也不会重复发号，只是编号不好看——但没必要留着这个意外。）
        with _id_lock:
            _id_counter = 0


def _clean_metadata(metadata: dict) -> dict:
    """Chroma 的 metadata 只接受 str / int / float / bool。

    list、None 之类一律转成字符串——否则会抛 ValueError。
    标签本来是列表，这里拼成逗号分隔的字符串存。
    """
    cleaned = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            cleaned[key] = ",".join(str(v) for v in value)
        elif isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned
