"""活动分工数据库：K4 成员 / 活动 / 参与记录。

这条线**不碰向量库**——都是结构化数据，全在 MySQL 里。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db, models, schemas

router = APIRouter(prefix="/api", tags=["活动分工数据库 K4"])


# ── 成员 ──────────────────────────────────────────────────────

@router.get("/members", response_model=list[schemas.MemberOut], summary="成员列表（脱敏）")
def list_members(
    keyword: str = Query("", description="按姓名或所属组模糊匹配"),
    session: Session = Depends(db.get_db),
):
    """成员列表。

    ★ 固定查 v_member_public 视图，绝不当场写字段清单。
      视图就是脱敏规则的唯一事实来源——学号/班级/生日/手机/微信/邮箱
      这些敏感字段根本不在视图里，所以这里想漏都漏不出去。
    """
    stmt = select(models.MemberPublic)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            models.MemberPublic.name.like(like) | models.MemberPublic.org_unit.like(like)
        )
    return session.scalars(stmt.order_by(models.MemberPublic.id)).all()


@router.get("/members/{member_id}", response_model=schemas.MemberOut, summary="成员详情（脱敏）")
def get_member(member_id: int, session: Session = Depends(db.get_db)):
    """注意这里也走视图。要看敏感字段是另一个接口的事（需权限）。"""
    member = session.scalars(
        select(models.MemberPublic).where(models.MemberPublic.id == member_id)
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return member


@router.post("/members", status_code=201, summary="新增成员")
def create_member(payload: schemas.MemberCreate, session: Session = Depends(db.get_db)):
    member = models.Member(**payload.model_dump())
    session.add(member)
    session.commit()
    session.refresh(member)
    # 只回 id，不回整条记录——省得把敏感字段顺手返回出去
    return {"ok": True, "id": member.id}


# ── 活动系列 ──────────────────────────────────────────────────

@router.get("/series", response_model=list[schemas.SeriesOut], summary="活动系列列表")
def list_series(session: Session = Depends(db.get_db)):
    return session.scalars(select(models.ActivitySeries).order_by(models.ActivitySeries.id)).all()


@router.post("/series", status_code=201, summary="新增活动系列")
def create_series(payload: schemas.SeriesCreate, session: Session = Depends(db.get_db)):
    series = models.ActivitySeries(**payload.model_dump())
    session.add(series)
    session.commit()
    session.refresh(series)
    return {"ok": True, "id": series.id}


# ── 活动场次 ──────────────────────────────────────────────────

@router.get("/activities", response_model=list[schemas.ActivityOut], summary="活动场次列表")
def list_activities(
    include_cancelled: bool = Query(False, description="是否包含已取消的场次"),
    session: Session = Depends(db.get_db),
):
    stmt = select(models.Activity)
    if not include_cancelled:
        stmt = stmt.where(models.Activity.status != "取消")
    return session.scalars(stmt.order_by(models.Activity.event_date)).all()


@router.post("/activities", status_code=201, summary="新增活动场次")
def create_activity(payload: schemas.ActivityCreate, session: Session = Depends(db.get_db)):
    activity = models.Activity(**payload.model_dump())
    session.add(activity)
    session.commit()
    session.refresh(activity)
    return {"ok": True, "id": activity.id}


# ── 参与记录 ──────────────────────────────────────────────────

@router.get("/participations", response_model=list[schemas.ParticipationOut], summary="参与记录列表")
def list_participations(
    activity_id: int | None = Query(None),
    member_id: int | None = Query(None),
    session: Session = Depends(db.get_db),
):
    stmt = select(models.Participation)
    if activity_id is not None:
        stmt = stmt.where(models.Participation.activity_id == activity_id)
    if member_id is not None:
        stmt = stmt.where(models.Participation.member_id == member_id)
    return session.scalars(stmt.order_by(models.Participation.id)).all()


@router.post("/participations", status_code=201, summary="新增参与记录")
def create_participation(payload: schemas.ParticipationCreate, session: Session = Depends(db.get_db)):
    """登记「谁 + 哪个活动 + 什么分工」。

    ⚠️ 唯一键是 (activity_id, member_id, role)。
       同一个人在同一个活动里可以有多条记录，只要角色不同
       ——既当主讲又当执行的，就是两条。
       但完全相同的三元组会撞唯一键，MySQL 会抛 IntegrityError，
       这里提前查一次，返回一句能看懂的话。
    """
    # 先确认这人和这场活动都真实存在。
    #
    # ⚠️ 不查的话，INSERT 会撞上 fk_part_activity / fk_part_member 外键，
    #    SQLAlchemy 抛 IntegrityError，没人接 → HTTP 500「Internal Server Error」。
    #    对调用方（尤其是 QQ 机器人）来说这是最没用的一种回复：它只知道
    #    「服务出错了」，不知道是自己把 activity_id 传错了，只能干瞪眼。
    #
    #    experience.py 里对 author_id / activity_id 就是这么查的，这里保持一致。
    if session.get(models.Activity, payload.activity_id) is None:
        raise HTTPException(status_code=400, detail=f"activity_id={payload.activity_id} 不存在")
    if session.get(models.Member, payload.member_id) is None:
        raise HTTPException(status_code=400, detail=f"member_id={payload.member_id} 不存在")

    exists = session.scalars(
        select(models.Participation).where(
            models.Participation.activity_id == payload.activity_id,
            models.Participation.member_id == payload.member_id,
            models.Participation.role == payload.role,
        )
    ).first()
    if exists is not None:
        raise HTTPException(
            status_code=409,
            detail=f"成员 {payload.member_id} 在活动 {payload.activity_id} 已经是「{payload.role}」了，不用重复登记",
        )

    participation = models.Participation(**payload.model_dump())
    session.add(participation)
    session.commit()
    session.refresh(participation)
    return {"ok": True, "id": participation.id}
