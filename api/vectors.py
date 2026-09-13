"""向量库：Chroma（落盘）+ sentence-transformers（生成向量）。

═══ 三个坑在这里落地 ═══
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

# 模块级缓存：模型和集合都只初始化一次。
# 模型加载要 15–30 秒，每次请求都加载的话服务就没法用了。
_model: SentenceTransformer | None = None
_collection: Any = None

# 发号用的锁和计数器，见 next_id() 的说明。
_id_lock = threading.Lock()
_id_counter = 0


def get_model() -> SentenceTransformer:
    """加载 embedding 模型（首次调用会下载，之后走本地缓存）。"""
    global _model
    if _model is None:
        _model = SentenceTransformer(config.EMBEDDING_MODEL)
    return _model


def get_collection():
    """拿到 Chroma 集合（落盘，重启不丢）。"""
    global _collection
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
    """新增一条经验：正文进 documents，元信息进 metadata。"""
    collection = get_collection()
    collection.add(
        ids=[exp_id],
        documents=[content],
        embeddings=encode([content]),
        metadatas=[_clean_metadata(metadata)],
    )


def update(exp_id: str, content: str, metadata: dict) -> None:
    collection = get_collection()
    collection.update(
        ids=[exp_id],
        documents=[content],
        embeddings=encode([content]),
        metadatas=[_clean_metadata(metadata)],
    )


def delete(exp_id: str) -> None:
    get_collection().delete(ids=[exp_id])


# ── 读 ────────────────────────────────────────────────────────

def search(query: str, top_k: int = 5) -> list[dict]:
    """语义检索：输入一句话，返回最像的 top_k 条。

    注意用的是 query_embeddings 而不是 query_texts。
    用 query_texts 的话 Chroma 会拿它自带的 all-MiniLM-L6-v2（384 维）去编码，
    和我们入库用的 bge-small-zh（512 维）不是一个模型，维度对不上直接报错；
    就算维度碰巧一样，语义空间也不是同一个，结果没有意义。
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    # 坑 2：这里不加 instruction 前缀，和入库时的写法保持一致。
    query_vector = encode([query])[0]

    result = collection.query(
        query_embeddings=[query_vector],
        n_results=min(top_k, collection.count()),
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
    """
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


def list_all(limit: int = 100, offset: int = 0) -> list[dict]:
    """列出经验（Chroma 没有分页，这里取全量后切片）。"""
    result = get_collection().get()
    items = [
        {
            "exp_id": i,
            "content": d,
            "metadata": dict(m or {}),
        }
        for i, d, m in zip(result["ids"], result["documents"], result["metadatas"])
    ]
    items.sort(key=lambda x: x["exp_id"])
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
    """
    global _id_counter
    with _id_lock:
        used = 0
        for i in get_collection().get()["ids"]:
            try:
                used = max(used, int(i.split("-")[1]))
            except (IndexError, ValueError):
                continue
        _id_counter = max(_id_counter, used) + 1
        return f"exp-{_id_counter}"


def clear() -> None:
    """清空集合（seed 重建时用）。"""
    global _collection, _id_counter
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
