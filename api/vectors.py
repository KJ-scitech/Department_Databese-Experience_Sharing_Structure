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

─── 另加的一道自检（不是新坑，是坑 1 的保险丝）───
    get_or_create_collection 对**已存在**的集合会直接拿来用，把新传进去的
    metadata 丢掉，一个字都不说。所以磁盘上那个集合如果是早先按默认的 l2
    建出来的，上面 坑 1 那句 metadata={"hnsw:space": "cosine"} 就完全是
    白写的——分数全错、依然不报错。

    所以启动时会回头问集合一句「你自己记着的距离空间是哪个」，
    不是 cosine 就直接抛异常：宁可起不来，也不要让它静静地输出错数字。
    见 _assert_cosine_space()。
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

# PersistentClient 也缓存一份。Chroma 内部按 path 缓存客户端，本来不会真开
# 第二份，但显式持有更好读，也让 clear() 拿到的是同一个对象而不是另造一个。
_client: Any = None


class DuplicateExpId(Exception):
    """id 已经存在。

    为什么要单独定义这一类，而不让 Chroma 那个 ValueError 直接穿到路由：
    Chroma 对「id 重复」和「metadata 里塞了它不认的类型」抛的**是同一个**
    ValueError，路由分不清是哪种，只能笼统地给个 500。
    分开之后：「id 撞了」是 409（调用方换个 id 还能重试），
    别的才留给 500（那就该去看日志了）。
    """


def _get_client():
    """拿 PersistentClient（模块级只造一次，见上面的 _client）。"""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    return _client


def _assert_cosine_space(collection) -> None:
    """确认这个集合真的建在余弦空间上——坑 1 的保险丝。

    get_or_create_collection 只有「新建」那一次才会用到传进去的 metadata；
    集合已经在磁盘上时它直接用，**不比对、不覆盖、不提醒**。
    于是「早期版本建的 l2 集合」会一直用下去：距离是平方欧氏距离，
    1 - distance 算出来还是个长得挺像那么回事的数（比如 0.13），
    页面照常显示、检索照常返回——没有任何地方会喊一声。

    这正是坑 1 最毒的地方，所以这里主动读一次集合自己记着的空间，
    不是 cosine 就抛异常。异常信息里带上集合名和修法，别让人对着
    一句「空间不对」发呆。

    注：collection.metadata 是 Chroma 存下来的建集合时的 metadata。
    拿不准的机器上跑一行确认一下：
        python -c "from api import vectors; print(vectors.get_collection().metadata)"
    正常应该看到 {'hnsw:space': 'cosine'}。
    """
    space = (collection.metadata or {}).get("hnsw:space")
    if space is None or str(space).lower() != "cosine":
        raise RuntimeError(
            f"向量集合 {collection.name!r} 的距离空间不是 cosine（读到的值是 {space!r}）。"
            "本项目的相似度 = 1 - distance 只在 cosine 空间成立（见文件开头 坑 1）。"
            f"修法：删掉 {config.CHROMA_DIR} 下的集合重新灌数据，"
            "或调 vectors.clear() 后重新 seed。不要绕过这个检查。"
        )


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
                client = _get_client()

                _collection = client.get_or_create_collection(
                    name=config.COLLECTION_NAME,          # 坑 3：ASCII 名
                    metadata={"hnsw:space": "cosine"},    # 坑 1：显式指定余弦空间
                )

                # ⚠️ 上面那行 metadata 只对「新建集合」这一种情况生效。
                # 集合已经在磁盘上时，Chroma 会直接用那个、把 metadata 丢掉，
                # 不报错也不提示。所以必须再回头确认一次它自己记着的是什么空间，
                # 见 _assert_cosine_space 与文件开头那段说明。
                _assert_cosine_space(_collection)
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

    （这一句的实现细节：现在会先转成 DuplicateExpId 再抛，路由据此回 409
    而不是 500——见下面 except 处和 _as_duplicate 的说明。）
    """
    collection = get_collection()
    try:
        collection.add(
            ids=[exp_id],
            documents=[content],
            embeddings=encode([content]),
            metadatas=[_clean_metadata(metadata)],
        )
    except ValueError as exc:
        raise _as_duplicate(collection, [exp_id], exc) from exc


def add_batch(items: list[dict]) -> int:
    """批量新增（seed / 导数据用），返回写入条数。

    逐条调 add() 的话，每条都要过一次模型——虽然模型已缓存，
    但 encode 的分词、推理开销乘以条数还是很可观；批量一次 encode
    能快一个数量级。空列表直接返回 0，Chroma 不接受空写入。

    每项形如 {"exp_id": ..., "content": ..., "metadata": {...}}。
    """
    if not items:
        return 0

    ids = [item["exp_id"] for item in items]
    # 先自查一遍批内重复。Chroma 遇到重复 id 是「整批都不写」，
    # 报出来的错只说有重复、不说是哪一条，seed 数据几百条时够找的。
    # 在这里点名，能直接看到是哪个 id 被写了两遍。
    if len(set(ids)) != len(ids):
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        raise DuplicateExpId(f"这一批里 id 重复了: {', '.join(repeated)}")

    collection = get_collection()
    try:
        collection.add(
            ids=ids,
            documents=[item["content"] for item in items],
            embeddings=encode([item["content"] for item in items]),
            metadatas=[_clean_metadata(item.get("metadata") or {}) for item in items],
        )
    except ValueError as exc:
        raise _as_duplicate(collection, ids, exc) from exc
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

    读回的顺序：先 update 再 get，一共只查一次。
    反过来的话（先 get 确认存在、再 update、再 get 读回）要查两次，
    而这里 update 对不存在的 id 是**静默成功**的，所以「查不到就当不存在」
    这个判断放在 update 之后同样成立——省掉的那一次 get 是白省的。
    """
    collection = get_collection()
    collection.update(ids=[exp_id], metadatas=[_clean_metadata({"tags": tags})])
    result = collection.get(ids=[exp_id])
    if not result["ids"]:
        raise KeyError(f"经验不存在: {exp_id}")
    return {
        "exp_id": result["ids"][0],
        "content": result["documents"][0],
        "metadata": dict(result["metadatas"][0] or {}),
    }


def delete(exp_id: str) -> None:
    """删除一条经验。id 不存在时 Chroma 静默成功——语义上「删没了」也算删成。"""
    get_collection().delete(ids=[exp_id])


# ── 读 ────────────────────────────────────────────────────────

def search(query: str, top_k: int = 5, *, type: str | None = None, author_id: int | None = None) -> list[dict]:
    """语义检索：输入一句话，返回最像的 top_k 条。

    注意用的是 query_embeddings 而不是 query_texts。
    用 query_texts 的话 Chroma 会拿它自带的 all-MiniLM-L6-v2（384 维）去编码，
    和我们入库用的 bge-small-zh（512 维）不是一个模型，维度对不上直接报错；
    就算维度碰巧一样，语义空间也不是同一个，结果没有意义。

    加了两个可选筛选（查某一类 / 某个人的经验）：它在**库里**筛、
    在取出 top_k 之前筛，所以「筛完还剩几条就是几条」，
    不会出现「先拿 5 条再把无关的丢掉，只剩 1 条」那种越筛越空的情形。
    不传就是原来的行为，一个字没变。

    筛选条件里没有标签：tags 在库里是逗号串，Chroma 的 where 只支持
    「等于」不支持子串匹配，筛不了（见 _build_where 的说明）。
    """
    if top_k < 1:
        return []

    collection = get_collection()
    total = collection.count()  # 只查一次，下面两处都要用
    if total == 0:
        return []

    # 坑 2：这里不加 instruction 前缀，和入库时的写法保持一致。
    query_vector = encode([query])[0]

    kwargs: dict[str, Any] = {
        "query_embeddings": [query_vector],
        "n_results": min(top_k, total),
    }
    where = _build_where(type=type, author_id=author_id)
    if where is not None:
        # 传 where=None 和不传 where 在 Chroma 上应该是一回事，但这是
        # 别人库里的实现细节，不值得赌——没有条件就干脆别放这个键。
        kwargs["where"] = where

    result = collection.query(**kwargs)

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


def list_all(
    limit: int = 100,
    offset: int = 0,
    *,
    type: str | None = None,
    tag: str | None = None,
    author_id: int | None = None,
) -> list[dict]:
    """列出经验（Chroma 没有分页，这里取全量后切片）。

    include 只取 documents 和 metadatas——默认还会带 embeddings（每条 512 个
    float），几百条经验就是几十 MB 的无用拷贝，纯粹浪费内存和时间。

    三个可选筛选（前端「按条件筛选」要的）：**先筛、再排序、最后切片**，
    顺序不能反。反了的话 offset 是在未筛选的全量上算的，
    第 2 页会莫名其妙少几条或多几条，而且不报错。
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
    items = _filter_items(items, type=type, tag=tag, author_id=author_id)
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
        client = _get_client()
        try:
            client.delete_collection(config.COLLECTION_NAME)
        except Exception:
            pass
        _collection = None
        # 集合没了，号也得从头开始，否则 seed 造出来的第一条会是 exp-9 而不是 exp-1。
        # （不清零也不会重复发号，只是编号不好看——但没必要留着这个意外。）
        with _id_lock:
            _id_counter = 0


def tags_of(metadata: dict) -> list[str]:
    """把 metadata 里存着的标签串拆回列表。

    标签在库里是逗号串（Chroma 的 metadata 只收标量），拆开这件事
    路由层也要做。放在这里导出，就只有一个地方知道「逗号 + 空白 = 分隔符」——
    否则三处各写一遍，哪天改存法必定漏掉一处，而漏掉的那处照样能跑，
    只是标签看起来怪怪的，很难被发现。
    """
    tags = (metadata or {}).get("tags")
    if isinstance(tags, str):
        return [t for t in (x.strip() for x in tags.split(",")) if t]
    if isinstance(tags, (list, tuple)):
        return [str(t) for t in tags]
    return []


def _build_where(type: str | None = None, author_id: int | None = None) -> dict | None:
    """把筛选条件拼成 Chroma 的 where。

    写法是 {字段: {"$eq": 值}}；两个以上条件必须显式用 "$and" 包起来——
    直接把两个键塞进同一个 dict 看着也能跑（隐式 and），但那属于
    不同版本行为不完全一致的写法，别赌。一个条件都没有就返回 None。

    标签筛不了：tags 在库里是拼好的逗号串，Chroma 的 where 只支持
    「等于」，没有子串匹配（$contains 属于 where_document，那是搜正文的）。
    要按标签筛就走 list_all，它在内存里筛。
    """
    clauses = []
    if type is not None:
        clauses.append({"type": {"$eq": type}})
    if author_id is not None:
        clauses.append({"author_id": {"$eq": author_id}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _as_duplicate(collection, ids: list[str], exc: ValueError) -> Exception:
    """把 Chroma 的 ValueError 分成「撞 id」和「别的问题」。

    这两件事 Chroma 抛的是同一个 ValueError 类，只能自己回查一次：
    查得到 → 就是撞 id，返回 DuplicateExpId（路由转 409）；
    查不到 → 是别的毛病（最常见是 metadata 里塞了它不认的类型），
    原样把异常还回去。**别把它也伪装成「id 重复」**——
    不然排查方向会被带偏，而这种错通常只在 seed 时才出现。
    """
    if collection.get(ids=ids)["ids"]:
        return DuplicateExpId(f"经验 id 已存在: {', '.join(ids)}")
    return exc


def _int_or_none(value) -> int | None:
    """元信息里的 id 转成 int；转不了返回 None。

    库里存的是原始类型，正常是 int；但别人手工 seed 过、或者哪天
    存成了字符串，都不该让筛选整个崩掉——筛不出来就是「这条不匹配」。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _filter_items(
    items: list[dict],
    *,
    type: str | None = None,
    tag: str | None = None,
    author_id: int | None = None,
) -> list[dict]:
    """在内存里过筛选条件（list_all 用）。

    为什么不交给 Chroma 的 where：tags 在库里是拼好的逗号串，
    Chroma 的 where 只支持「等于」、不支持子串包含，标签这个条件它做不了；
    而 list_all 本来就要取全量再切片，内存里过一遍不额外花钱。

    三个条件都给 None 时原样返回——所有老调用方（CLI 的翻页、
    seed 的自查）行为一个字都不变。
    """
    if type is not None:
        items = [i for i in items if i["metadata"].get("type") == type]
    if author_id is not None:
        items = [i for i in items if _int_or_none(i["metadata"].get("author_id")) == author_id]
    if tag is not None:
        items = [i for i in items if tag in tags_of(i["metadata"])]
    return items


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
