"""经验知识库：K1 录入 / K2 语义检索 / K3 元信息 / K8 修改标签。

这一条线**不碰 MySQL 的业务数据**——经验本体存在 Chroma 里。
和 MySQL 的联系只靠 id：元信息里记 author_id / activity_id /
participation_id，这些 id 和 MySQL 的 member.id / activity.id /
participation.id 是同一套。

═══ 这个文件里最容易出错的两件事 ═══

坑 A · 路由顺序不能动
    /search、/keyword 必须写在 /{exp_id} **前面**。FastAPI 是按声明顺序
    匹配的，/{exp_id} 会把 "search" 当成一个经验 id 吃下去，
    于是 GET /api/experience/search 变成 404「经验 search 不存在」。

坑 B · 两条库没有共同的事务
    向量库和 MySQL 是两个独立的存储，谁也回滚不了谁。所有「先写一边、
    再写另一边」的地方都必须想清楚：中途失败会留下什么？
    下面 create / delete 都按这个思路做了收尾，改动时别把它删掉。
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import db, models, schemas
from .. import vectors as vec

router = APIRouter(prefix="/api/experience", tags=["经验知识库 K1-K3"])

# 标签的体量上限。防的不是恶意攻击，是「机器人/脚本一次塞进来 300 个标签」
# 这种手滑——存进去之后没人会去清，检索也会被噪声带偏。
MAX_TAGS = 12
MAX_TAG_LEN = 20


class TagsUpdate(BaseModel):
    """K8 的 PATCH 请求体：**只接受 tags**（契约见任务拆解 §4.9）。

    为什么不写进 api/schemas.py：那是共享文件，改之前要在组里说一声（§六.6）。
    这个模型只有 K8 一个消费者，放在路由文件里契约一样清楚，还不用让别人等。

    extra="forbid" 是契约里那句「别的字段不开放」的落地：传
    {"tags": [...], "content": "改一改"} 会直接 422 报错，
    而不是把 content **静默忽略**——静默忽略会让调用方以为自己改成功了。
    """

    model_config = {"extra": "forbid"}

    tags: list[str] = Field(..., max_length=MAX_TAGS, description="整份替换，最多 12 个")


# ── 小工具 ────────────────────────────────────────────────────

def _clean_tags(tags: list[str]) -> list[str]:
    """去掉空白和空串、去重（保序）、截断超长标签。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tags:
        tag = (raw or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag[:MAX_TAG_LEN])
    return cleaned[:MAX_TAGS]


def _split_tags(metadata: dict) -> dict:
    """Chroma 的 metadata 只能存标量，入库时把标签拼成了逗号串。

    返回给前端时再拆回列表，前端好渲染。
    已经是列表的原样返回——create 那里用的是还没入库的本地 metadata，
    标签还是 list，不该被这个函数再拆一次。
    """
    meta = dict(metadata)
    tags = meta.get("tags")
    if isinstance(tags, str):
        meta["tags"] = [t for t in (x.strip() for x in tags.split(",")) if t]
    elif tags is None:
        meta["tags"] = []
    elif isinstance(tags, (list, tuple)):
        meta["tags"] = [str(t) for t in tags]
    return meta


def _as_int(value: Any) -> int | None:
    """元信息里取出来的 id 一定是数字吗？不一定。

    Chroma 的 metadata 存的是原始类型，理论上 author_id 是 int；
    但库可能是别人 seed 进去的、或者手工改过的，取到字符串甚至乱码
    都不奇怪。裸 int() 遇到这种就抛 ValueError → 500，
    而正确的行为是「这个人查不到」，不是「整个接口崩了」。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enrich_names(metadata: dict, session: Session | None) -> dict:
    """把 author_id / activity_id 翻译成人能看懂的名字（K3 的「双库并存」）。

    session 为 None 时直接返回——检索结果不补名字，一次查询补 N 次
    MySQL 是 N+1，列表页会明显变慢，而列表页本来就只要 id。
    """
    if session is None:
        return metadata

    author_id = _as_int(metadata.get("author_id"))
    if author_id is not None:
        member = session.get(models.Member, author_id)
        metadata["author_name"] = member.name if member else None

    activity_id = _as_int(metadata.get("activity_id"))
    if activity_id is not None:
        activity = session.get(models.Activity, activity_id)
        metadata["activity_name"] = activity.name if activity else None

    return metadata


def _require_participation(session: Session, participation_id: Any) -> models.Participation | None:
    """校验 participation_id 存在，并**返回对象本身**。

    校验和取对象合在一起，是为了少查一次库——原来校验一次、
    后面要用时再查一次，同一个 id 查两遍。
    """
    if participation_id is None:
        return None
    participation = session.get(models.Participation, participation_id)
    if participation is None:
        raise HTTPException(status_code=400, detail=f"participation_id={participation_id} 不存在")
    return participation


def _check_refs_exist(session: Session, payload: schemas.ExperienceCreate) -> models.Participation | None:
    """录入前置校验：author_id / activity_id / participation_id 必须真实存在。

    不校验的话会造出「孤儿经验」：向量库里有这条经验，
    它指向的成员/活动在 MySQL 里根本没有。这类脏数据事后极难清理——
    界面上看不出，统计里凭空多一条，谁也不知道该删谁。
    """
    if payload.author_id is not None and session.get(models.Member, payload.author_id) is None:
        raise HTTPException(status_code=400, detail=f"author_id={payload.author_id} 不存在")
    if payload.activity_id is not None and session.get(models.Activity, payload.activity_id) is None:
        raise HTTPException(status_code=400, detail=f"activity_id={payload.activity_id} 不存在")
    return _require_participation(session, payload.participation_id)


# ── K1 录入 ───────────────────────────────────────────────────

@router.post("", response_model=schemas.ExperienceOut, summary="K1 录入经验")
def create_experience(payload: schemas.ExperienceCreate, session: Session = Depends(db.get_db)):
    """录入一条经验。

    除了写向量库，还会做两件事：
      1. 校验 author_id / activity_id 在 MySQL 里真实存在（防止造出孤儿引用）
      2. 如果带了 participation_id，把新经验的 id 追加进那条参与记录的 exp_ids 里
         —— 这就是 schema 里 exp_ids 字段的用途
    """
    if payload.type not in schemas.EXPERIENCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type 只能是 {'/'.join(schemas.EXPERIENCE_TYPES)}",
        )

    participation = _check_refs_exist(session, payload)

    tags = _clean_tags(payload.tags or [])
    exp_id = vec.next_id()
    metadata = {
        "type": payload.type,
        "tags": tags,
        "author_id": payload.author_id,
        "activity_id": payload.activity_id,
        "participation_id": payload.participation_id,
        "source": payload.source,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    vec.add(exp_id, payload.content, metadata)

    # 反向关联：参与记录 → 经验
    if participation is not None:
        try:
            linked = list(participation.exp_ids or [])
            linked.append(exp_id)
            participation.exp_ids = linked   # 重新赋值（不是 append 到原列表），JSON 列才会被判定为已改
            session.commit()
        except Exception:
            # 向量库里已经写进去了，MySQL 这边没挂上。两条库没有共同事务，
            # 只能退一步：把刚写的那条向量删掉，让「录入」整体失败。
            # 反过来留着它，就是一条谁都关联不上的孤儿经验——而 exp_ids
            # 少了这一条，界面上看不出来，等发现时已经不知道该怎么补。
            session.rollback()
            try:
                vec.delete(exp_id)
            except Exception:
                pass  # 兜底的兜底：删不掉也只能让它去，至少已经把错误抛出去了
            raise HTTPException(status_code=500, detail="写入参与记录失败，本次录入已回滚")

    # 直接用本地的 metadata 拼返回，不再回查一次向量库：
    # 刚写进去的内容我们手上就有，回查只是多一次 IO。
    return {
        "exp_id": exp_id,
        "content": payload.content,
        "metadata": _split_tags(metadata),
    }


# ── K2 检索 ───────────────────────────────────────────────────

@router.get("/search", response_model=list[schemas.SearchHit], summary="K2 语义检索")
def search_experience(
    q: str = Query(..., min_length=1, description="自然语言问句，如「设备出故障了咋办」"),
    top_k: int = Query(5, ge=1, le=20),
):
    """用自然语言召回相关经验。

    这是整个 demo 的核心。注意它是**语义**检索：
    问「设备出故障了咋办」能召回「投影仪不亮」那条，
    但按关键词搜「设备」一条都搜不到——两者字面上没有共同词。

    这个接口**不做阈值过滤**（任务拆解 §1.3）：像与不像的分数是重叠的，
    一刀切不准。全量返回 + 带相似度，由调用方决定怎么说——
    网页显示数字给人判断，机器人按两档话术（说「没找到」/ 说「把握不大」）。
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="q 不能只有空白字符")

    hits = vec.search(q, top_k=top_k)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


@router.get("/keyword", response_model=list[schemas.KeywordHit], summary="关键词对照（非向量检索）")
def keyword_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
):
    """字面包含查找，用来和上面的语义检索做对照。

    同一个问句「设备出故障了咋办」丢给这个接口，会返回 0 条；
    丢给 /search 却能召回「投影仪不亮」。
    这就是为什么经验库要用向量检索——大家记笔记时的用词根本对不上。

    它是**对照用的**，别拿它当检索的主路径（大小写敏感、只认精确子串）。
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="q 不能只有空白字符")

    hits = vec.keyword_search(q, limit=limit)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


# ── 列表 ──────────────────────────────────────────────────────

@router.get("", response_model=list[schemas.ExperienceOut], summary="经验列表（分页）")
def list_experiences(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """按 exp_id 顺序分页。

    排序用的是 exp_id 的**数字序**（见 vectors.id_num）——
    Chroma 没有分页，这里是取全量后切片，排序错一点第 2 页就会重复/漏条目。
    """
    items = vec.list_all(limit=limit, offset=offset)
    for item in items:
        item["metadata"] = _split_tags(item["metadata"])
    return items


# ── K3 元信息 ─────────────────────────────────────────────────
# ⚠️ /{exp_id} 这类通配路由必须写在所有固定路径之后，见文件开头「坑 A」。

@router.get("/{exp_id}", response_model=schemas.ExperienceOut, summary="K3 经验元信息")
def get_experience(exp_id: str, session: Session = Depends(db.get_db)):
    """按 id 取单条经验，并把 author_id / activity_id 翻译成人能看懂的名字。

    经验本体不在 MySQL 里，所以这里要拿元信息里的 id 回查 MySQL 补名称。
    这正是需求文档说的「双库并存，通过 id 关联」。
    """
    item = vec.get(exp_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")

    metadata = _enrich_names(_split_tags(item["metadata"]), session)
    return {"exp_id": item["exp_id"], "content": item["content"], "metadata": metadata}


# ── K8 修改标签（契约见任务拆解 §4.9）─────────────────────────

@router.patch("/{exp_id}", response_model=schemas.ExperienceOut, summary="修改经验标签（K8）")
def update_experience_tags(exp_id: str, payload: TagsUpdate, session: Session = Depends(db.get_db)):
    """只改标签，返回和 GET /api/experience/{exp_id} 一样的 ExperienceOut。

    契约：请求体 {"tags": [...]} → 200 ExperienceOut；没这条经验 → 404。

    为什么不做成「读出来 → 改 tags → 整条写回」：正文和向量会在写回时
    被重新编码，多一次模型推理，还平白多一次覆盖别人并发改动的机会。
    vectors.update_tags 只写 metadata，正文一个字节都不动。

    ⚠️ 调用方（K8 打标脚本）必须自己保证「只补空的、不覆盖人工标签」——
    这是入口的语义（整份替换），不是这里的责任。
    """
    tags = _clean_tags(payload.tags)
    if not tags:
        raise HTTPException(status_code=400, detail="tags 至少要有一个非空标签")

    try:
        item = vec.update_tags(exp_id, tags)
    except KeyError:
        # vectors 那边对不存在的 id 抛 KeyError → 契约要求的 404。
        # 不能不管：Chroma 的 update 对不存在的 id 是**静默成功**的。
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")

    metadata = _enrich_names(_split_tags(item["metadata"]), session)
    return {"exp_id": item["exp_id"], "content": item["content"], "metadata": metadata}


# ── 删除 ──────────────────────────────────────────────────────

@router.delete("/{exp_id}", summary="删除经验")
def delete_experience(exp_id: str, session: Session = Depends(db.get_db)):
    """删除经验，并把参与记录里的反向引用一起摘掉。

    顺序是**先断 MySQL 的链，再删向量**。反过来的话：向量删掉了、
    commit 失败了，就留下 participation.exp_ids 里一个指向不存在经验的 id——
    报表和履历里会凭空多出一条查不出内容的记录，而且永远不报错。
    按这个顺序，最坏情况只是「经验还在、没挂上参与记录」，看得见、能补。
    """
    item = vec.get(exp_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")

    participation_id = _as_int(item["metadata"].get("participation_id"))
    if participation_id is not None:
        participation = session.get(models.Participation, participation_id)
        linked = list(participation.exp_ids or []) if participation is not None else []
        if exp_id in linked:
            participation.exp_ids = [i for i in linked if i != exp_id]
            session.commit()

    vec.delete(exp_id)
    return {"ok": True, "deleted": exp_id}
