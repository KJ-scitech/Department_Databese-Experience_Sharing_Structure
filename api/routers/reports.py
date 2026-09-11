"""报表与轮值统计：K5。

需求文档原话：按活动/学期出报表；统计谁还没参与过、谁参与了几轮，
用于安排轮值。

⚠️ 报表**不做时长发放**。五育/志愿时长不由本系统产出，
   这里只做「谁干了几次、干了什么角色」的统计。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import db, models, schemas

router = APIRouter(prefix="/api", tags=["报表与轮值 K5"])


@router.get("/reports/by-member", response_model=list[schemas.MemberWorkload], summary="按人聚合工作量")
def report_by_member(
    only_active: bool = True,
    session: Session = Depends(db.get_db),
):
    """每个人参与了几次、各是什么角色、拿过几次荣誉。

    用 LEFT JOIN 而不是 INNER JOIN——**一次都没参与过的人也要出现在结果里**，
    次数显示 0。用 INNER JOIN 的话这些人会直接从报表里消失，
    而「谁还没干过」恰恰是轮值时最需要看到的信息。
    """
    stmt = (
        select(
            models.Member.id,
            models.Member.name,
            func.count(models.Participation.id).label("total"),
        )
        .join(models.Participation, models.Participation.member_id == models.Member.id, isouter=True)
        .where(models.Member.deleted_at.is_(None))
        .group_by(models.Member.id, models.Member.name)
        .order_by(text("total DESC"), models.Member.id)
    )
    if only_active:
        stmt = stmt.where(models.Member.status == "在部")

    rows = session.execute(stmt).all()

    # 角色分布和荣誉次数另外聚合一次。
    # 为什么不塞进上面那条 SQL 里：角色是多值维度，硬拼进同一条
    # 会出现笛卡尔积，count 出来的数会偏大。
    role_rows = session.execute(
        select(
            models.Participation.member_id,
            models.Participation.role,
            func.count(models.Participation.id),
        ).group_by(models.Participation.member_id, models.Participation.role)
    ).all()
    roles_by_member: dict[int, dict[str, int]] = {}
    for member_id, role, n in role_rows:
        roles_by_member.setdefault(member_id, {})[role] = n

    honor_rows = session.execute(
        select(models.Participation.member_id, func.count(models.Participation.id))
        .where(models.Participation.honor.isnot(None))
        .group_by(models.Participation.member_id)
    ).all()
    honors_by_member = {member_id: n for member_id, n in honor_rows}

    return [
        {
            "member_id": member_id,
            "name": name,
            "total": total,
            "roles": roles_by_member.get(member_id, {}),
            "honors": honors_by_member.get(member_id, 0),
        }
        for member_id, name, total in rows
    ]


@router.get("/reports/never-participated", response_model=list[schemas.MemberOut], summary="谁还没参与过")
def report_never_participated(session: Session = Depends(db.get_db)):
    """一次都没参与过的人——安排轮值时先看这个。

    查的是 v_member_public 视图，所以返回的字段是脱敏过的。
    """
    participated = select(models.Participation.member_id).distinct()
    stmt = (
        select(models.MemberPublic)
        .where(
            models.MemberPublic.status == "在部",
            models.MemberPublic.id.notin_(participated),
        )
        .order_by(models.MemberPublic.id)
    )
    return session.scalars(stmt).all()


@router.get("/reports/rotation", summary="按学期统计参与分布")
def report_rotation(session: Session = Depends(db.get_db)):
    """按学期看：这个学期办了几场活动、有多少人参与、人均几场。

    用来判断轮值是否均衡——如果某个学期老是人均 5 场，
    说明活都压在少数人身上。
    """
    stmt = (
        select(
            models.Activity.term,
            func.count(func.distinct(models.Activity.id)).label("activities"),
            func.count(func.distinct(models.Participation.member_id)).label("members"),
            func.count(models.Participation.id).label("records"),
        )
        .join(models.Participation, models.Participation.activity_id == models.Activity.id, isouter=True)
        .where(models.Activity.status != "取消")
        .group_by(models.Activity.term)
        .order_by(models.Activity.term)
    )
    rows = session.execute(stmt).all()
    return [
        {
            "term": term or "（未填学期）",
            "activities": activities,
            "members": members,
            "records": records,
            # 人均场次 = 参与记录数 / 参与人数，四舍五入到 1 位小数
            "per_capita": round(records / members, 1) if members else 0,
        }
        for term, activities, members, records in rows
    ]


@router.get("/members/{member_id}/history", response_model=list[schemas.HistoryItem], summary="个人活动履历")
def member_history(member_id: int, session: Session = Depends(db.get_db)):
    """某个人的完整履历：参加过哪些活动、担任什么角色、关联了哪些经验。

    走 v_member_history 视图，四表连好的，不用自己写 JOIN。
    exp_ids 里的 exp-xxx 就是向量库里的经验 id，前端可以点进去看。
    """
    if session.get(models.Member, member_id) is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    stmt = (
        select(models.MemberHistory)
        .where(models.MemberHistory.member_id == member_id)
        .order_by(models.MemberHistory.event_date)
    )
    return session.scalars(stmt).all()
