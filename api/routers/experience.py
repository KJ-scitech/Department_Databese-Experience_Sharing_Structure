# -*- coding: utf-8 -*-
"""经验知识库：K1 录入 / K2 语义检索 / K3 元信息。
这一条线**不碰 MySQL**——经验本体存在 Chroma 里。
和 MySQL 的联系只靠 id：元信息里记 author_id / activity_id /
participation_id，这些 id 和 MySQL 的 member.id / activity.id /
participation.id 是同一套。
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from .. import db, models, schemas
from .. import vectors as vec

router = APIRouter(prefix="/api/experience", tags=["经验知识库 K1-K3"])


class TagsUpdate(BaseModel):
    """§4.9 契约：PATCH 只接受 tags，别的字段不开放。"""
    tags: list[str] = Field(default_factory=list, description="新的标签列表，完全替换旧标签")


def _split_tags(metadata: dict) -> dict:
    """Chroma 的 metadata 只能存标量，入库时把标签拼成了逗号串。
    返回给前端时再拆回列表，前端好渲染。
    """
    meta = dict(metadata)
    tags = meta.get("tags")
    if isinstance(tags, str):
        meta["tags"] = [t for t in tags.split(",") if t]
    elif tags is None:
        meta["tags"] = []
    return meta


@router.post("", response_model=schemas.ExperienceOut, summary="K1 录入经验")
def create_experience(payload: schemas.ExperienceCreate, session: Session = Depends(db.get_db)):
    """录入一条经验。
    除了写向量库，还会做两件事：
      1. 校验 author_id / activity_id 在 MySQL 里真实存在（防止造出孤儿引用）
      2. 如果带了 participation_id，把新经验的 id 追加进那条参与记录的 exp_ids 里
    """
    if payload.type not in schemas.EXPERIENCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type 只能是 {'/'.join(schemas.EXPERIENCE_TYPES)}",
        )
    if payload.author_id is not None:
        if session.get(models.Member, payload.author_id) is None:
            raise HTTPException(status_code=400, detail=f"author_id={payload.author_id} 不存在")
    if payload.activity_id is not None:
        if session.get(models.Activity, payload.activity_id) is None:
            raise HTTPException(status_code=400, detail=f"activity_id={payload.activity_id} 不存在")
    participation = None
    if payload.participation_id is not None:
        participation = session.get(models.Participation, payload.participation_id)
        if participation is None:
            raise HTTPException(status_code=400, detail=f"participation_id={payload.participation_id} 不存在")

    exp_id = vec.next_id()
    metadata = {
        "type": payload.type,
        "tags": payload.tags,
        "author_id": payload.author_id,
        "activity_id": payload.activity_id,
        "participation_id": payload.participation_id,
        "source": payload.source,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    vec.add(exp_id, payload.content, metadata)

    # 反向关联：参与记录 → 经验
    if participation is not None:
        linked = list(participation.exp_ids or [])
        linked.append(exp_id)
        participation.exp_ids = linked
        session.commit()

    return {"exp_id": exp_id, "content": payload.content,
            "metadata": _split_tags(vec.get(exp_id)["metadata"])}


@router.get("/search", response_model=list[schemas.SearchHit], summary="K2 语义检索")
def search_experience(
    q: str = Query(..., min_length=1, description="自然语言问句，如「设备出故障了咋办」"),
    top_k: int = Query(5, ge=1, le=20),
):
    """用自然语言召回相关经验。"""
    hits = vec.search(q, top_k=top_k)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


@router.get("/keyword", response_model=list[schemas.KeywordHit], summary="关键词对照（非向量检索）")
def keyword_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
):
    """字面包含查找，用来和语义检索做对照。"""
    hits = vec.keyword_search(q, limit=limit)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


@router.get("", response_model=list[schemas.ExperienceOut], summary="经验列表（分页）")
def list_experiences(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    items = vec.list_all(limit=limit, offset=offset)
    for item in items:
        item["metadata"] = _split_tags(item["metadata"])
    return items


@router.patch("/{exp_id}", response_model=schemas.ExperienceOut, summary="只改标签（K8 打标用）")
def patch_experience_tags(exp_id: str, payload: TagsUpdate, session: Session = Depends(db.get_db)):
    """§4.9 契约：
    - PATCH /api/experience/{exp_id}
    - 请求体只接受 {"tags": ["投影仪", "应急"]}，别的字段不开放
    - 返回和 GET /api/experience/{exp_id} 一样的 ExperienceOut
    - 404 = 没这条经验

    🔴 绝对不要删了重加——会换 exp_id，作者/活动/参与记录全断。
    只更新 metadata 里的 tags，不重新生成向量。
    """
    result = vec.update_tags(exp_id, payload.tags)
    if result is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")

    # 返回格式和 GET /{exp_id} 一致，补全 author_name / activity_name
    metadata = _split_tags(result["metadata"])
    author_id = metadata.get("author_id")
    if author_id:
        member = session.get(models.Member, int(author_id))
        metadata["author_name"] = member.name if member else None
    activity_id = metadata.get("activity_id")
    if activity_id:
        activity = session.get(models.Activity, int(activity_id))
        metadata["activity_name"] = activity.name if activity else None

    return {"exp_id": result["exp_id"], "content": result["content"], "metadata": metadata}


@router.get("/{exp_id}", response_model=schemas.ExperienceOut, summary="K3 经验元信息")
def get_experience(exp_id: str, session: Session = Depends(db.get_db)):
    """按 id 取单条经验，并把 author_id / activity_id 翻译成人能看懂的名字。"""
    item = vec.get(exp_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")
    metadata = _split_tags(item["metadata"])
    author_id = metadata.get("author_id")
    if author_id:
        member = session.get(models.Member, int(author_id))
        metadata["author_name"] = member.name if member else None
    activity_id = metadata.get("activity_id")
    if activity_id:
        activity = session.get(models.Activity, int(activity_id))
        metadata["activity_name"] = activity.name if activity else None
    return {"exp_id": item["exp_id"], "content": item["content"], "metadata": metadata}


@router.delete("/{exp_id}", summary="删除经验")
def delete_experience(exp_id: str):
    if vec.get(exp_id) is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")
    vec.delete(exp_id)
    return {"ok": True, "deleted": exp_id}

@router.delete("/{exp_id}", summary="删除经验")
def delete_experience(exp_id: str):
    if vec.get(exp_id) is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")
    vec.delete(exp_id)
    return {"ok": True, "deleted": exp_id}
