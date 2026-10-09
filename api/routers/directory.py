"""活动分工数据库：K4 成员 / 活动 / 参与记录。

这条线**不碰向量库**——都是结构化数据，全在 MySQL 里。

═══════════════════════════════════════════════════════════════════
  这次补的是什么（原本只有「增」和「查」）
═══════════════════════════════════════════════════════════════════

任务拆解 §5.3 给这条线写的是「成员 / 活动 / 参与记录的**增删查**接口」，
而接口上只有增和查。这次把缺的补齐：

  1. 成员列表筛选：届别 / 状态 / 组织 / 技能
     （需求文档 FR-1 点名要；验收标准第 2 条「按届别/组织/状态查询」靠它）
  2. 改：PATCH /members/{id}、PATCH /participations/{id}
  3. 删：DELETE /members/{id}（归档）、DELETE /activities/{id}、
         DELETE /series/{id}、DELETE /participations/{id}
  4. 需求文档里有、任务拆解里**没派给谁**的两件事，顺手补上（owner 觉得该归别人就说一声）：
     职务与换届（FR-4）、隐私授权（FR-6 / 验收标准第 5 条）

═══════════════════════════════════════════════════════════════════
  删除为什么不是一句 session.delete(...)
═══════════════════════════════════════════════════════════════════

- **删成员 = 归档（软删除）**，不是物理删除。
  报表要算「谁干过几场」，退部的人也得算进去；真删下去，
  participation 的外键 ON DELETE CASCADE 会把他的参与记录一起带走，
  **不报错**，报表直接少一块。所以只打一个 deleted_at，
  v_member_public 视图（WHERE deleted_at IS NULL）自动把他过滤掉。
- **删活动 / 删系列会连带动到别人的数据**，所以默认拦一道：
  返回 409 并说清「会连带删掉/解绑什么」，调用方确认了再带 force=true。
  宁可多问一句，也不要把别人的记录顺手删掉。
- 参与记录可以随便删——登错了删掉重记是最常见的操作，
  而且没有任何表引用它（经验那边是通过 exp_ids 字符串软引用的）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from .. import db, models, schemas

router = APIRouter(prefix="/api", tags=["活动分工数据库 K4"])


# ══ 新增的请求 / 响应模型为什么写在这里，而不是 api/schemas.py ══
#
# api/schemas.py 是**共享文件**：按 任务拆解.md §二 的规矩，改它之前要在组里说一声。
# 这次新增的都是本线自己的东西，就先放在本文件里，不去动共享文件；
# owner 看过觉得该并进去，整块挪到 schemas.py 即可（挪走时删掉这一段）。
#
# 已经在共享文件里的 MemberOut / SeriesOut / ActivityOut / ParticipationOut
# 继续用它们，这里不重复定义。


class MemberUpdate(BaseModel):
    """改成员：只发改动过的字段。

    字段集合和 `schemas.MemberCreate` 保持一致，免得出现
    「建的时候能填、改的时候反而不能填」这种别扭事。
    敏感字段（student_no / phone / email）允许改——录错了总得能修——
    但**响应里永远不会回显**（读侧一律走 v_member_public 视图）。
    """

    name: str | None = Field(None, min_length=1)
    gender: int | None = None
    grade: str | None = None
    major: str | None = None
    join_term: str | None = None
    status: str | None = None
    org_unit: str | None = None
    title: str | None = None
    member_type: str | None = None
    bio_short: str | None = None
    skills: list[str] | None = None
    student_no: str | None = None
    phone: str | None = None
    email: str | None = None


class ParticipationUpdate(BaseModel):
    """改一条参与记录。

    能改的是「这条记录写得对不对」：分工、贡献、工时、荣誉、关联经验。
    **不能改 activity_id / member_id**——那等于换了一条记录，
    删掉重登记就好；用改的话，事后没人看得出这条记录原本是谁的。
    """

    role: str | None = None
    contribution: str | None = None
    hours: float | None = None
    honor: str | None = None
    exp_ids: list[str] | None = None


class PositionCreate(BaseModel):
    member_id: int
    title: str = Field(..., min_length=1, description="部长/副部长/组长/组员")
    org_unit: str | None = None
    term: str = Field(..., min_length=1, description="任期学年，如 2026-2027")
    is_current: bool = True


class PositionUpdate(BaseModel):
    title: str | None = Field(None, min_length=1)
    org_unit: str | None = None
    term: str | None = Field(None, min_length=1)
    is_current: bool | None = None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    title: str
    org_unit: str | None = None
    term: str
    # 表里存的是 tinyint 1/0，对外给布尔值，前端不用再猜 1 是什么意思
    is_current: bool = False


class RolloverIn(BaseModel):
    term: str = Field(..., min_length=1, description="换届后的任期学年，如 2026-2027")


class RolloverOut(BaseModel):
    term: str
    current: int = Field(..., description="这一届里被标成现任的条数")
    cleared: int = Field(..., description="其余各届被取消现任的条数")


class ConsentCreate(BaseModel):
    """记一条隐私授权（需求文档 §5.1）。"""

    member_id: int
    consented: bool = True
    scope: str | None = Field(None, description="授权范围：入库 / 公开卡片")
    policy_version: str | None = Field(None, description="声明版本号")


class ConsentUpdate(BaseModel):
    """改授权状态。`consented=false` 就是「撤回同意」（§5.1 明确可撤回）。"""

    consented: bool | None = None
    scope: str | None = None
    policy_version: str | None = None


class ConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    consented: bool
    scope: str | None = None
    policy_version: str | None = None
    consented_at: datetime | None = None
    revoked_at: datetime | None = None


# ── 成员 ──────────────────────────────────────────────────────

@router.get("/members", response_model=list[schemas.MemberOut], summary="成员列表（脱敏）")
def list_members(
    keyword: str = Query("", description="按姓名或所属组模糊匹配"),
    join_term: str = Query("", description="按届别筛，如 2026-2027"),
    status: str = Query("", description="在部 / 退部 / 毕业 / 请假"),
    org_unit: str = Query("", description="按组筛"),
    skill: str = Query("", description="按技能标签筛，如 摄影"),
    session: Session = Depends(db.get_db),
):
    """成员列表。

    ★ 固定查 v_member_public 视图，绝不当场写字段清单。
      视图就是脱敏规则的唯一事实来源——学号/班级/生日/手机/微信/邮箱
      这些敏感字段根本不在视图里，所以这里想漏都漏不出去。

    ★ 后面四个筛选项对应需求文档 FR-1「按届别、状态、组织、技能筛选」，
      也是验收标准第 2 条「按届别/组织/状态查询」。
    """
    stmt = select(models.MemberPublic)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            models.MemberPublic.name.like(like) | models.MemberPublic.org_unit.like(like)
        )
    if join_term:
        stmt = stmt.where(models.MemberPublic.join_term == join_term)
    if status:
        stmt = stmt.where(models.MemberPublic.status == status)
    if org_unit:
        stmt = stmt.where(models.MemberPublic.org_unit == org_unit)
    if skill:
        # skills 是 JSON 数组。用 JSON_CONTAINS 更"正规"，但那个函数只在 MySQL 上有，
        # 本机 SQLite 的测试跑不了，两边行为会不一致；退一步转成字符串做文本匹配，
        # 两种库都能跑，逻辑也一眼看得懂。
        # ★ 匹配时把引号一起带上：`"摄影"` 不会命中 `"摄影后期"`，
        #   否则标签名互为子串时会互相污染（搜「摄影」把「摄影后期」也捞出来）。
        # ★ 显式 cast：JSON 列直接和字符串比，MySQL 上能不能隐式转换要看版本，
        #   cast 成字符串就没有这个不确定性（MySQL → CHAR，SQLite → VARCHAR）。
        stmt = stmt.where(cast(models.MemberPublic.skills, String).like(f'%"{skill}"%'))
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


@router.patch("/members/{member_id}", response_model=schemas.MemberOut, summary="修改成员")
def update_member(
    member_id: int,
    payload: MemberUpdate,
    session: Session = Depends(db.get_db),
):
    """改成员：只动传上来的字段（`exclude_unset=True`）。

    ⚠️ 不区分「没传」和「传了 null」的话，前端只想改个职务，
       就会把它没碰过的 phone / email 一起清空——这是个很难发现的静默数据损坏。

    ⚠️ 学号/工号是唯一键。改成别人在用的那个，MySQL 会抛 IntegrityError，
       落到调用方就是一句 500「Internal Server Error」。先查一遍，回 409 和一句人话。

    ★ 回的是视图那一行（脱敏），不是 member 表那一行——哪怕刚改过手机号，
      响应里也不会出现它。
    """
    member = session.get(models.Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    changes = payload.model_dump(exclude_unset=True)

    new_no = changes.get("student_no")
    if new_no:
        taken = session.scalars(
            select(models.Member).where(
                models.Member.student_no == new_no,
                models.Member.id != member_id,
            )
        ).first()
        if taken is not None:
            # 报错里**不写出这个学号**：错误信息经常被贴进群里问人，
            # 复述一遍就等于把敏感信息又发了一次
            raise HTTPException(status_code=409, detail="这个学号/工号已经被另一个成员占用了")

    for field, value in changes.items():
        setattr(member, field, value)
    session.commit()

    public = session.scalars(
        select(models.MemberPublic).where(models.MemberPublic.id == member_id)
    ).first()
    if public is None:  # 上面刚确认过他在，理论上到不了这里
        raise HTTPException(status_code=404, detail="成员不存在")
    return public


@router.delete("/members/{member_id}", summary="归档成员（软删除）")
def archive_member(member_id: int, session: Session = Depends(db.get_db)):
    """归档成员：人从名单里消失，**参与记录一条都不动**。

    ★ 为什么不做物理删除：分工统计要算「谁干过几场」，退部的人也得算进去。
      participation.member_id 的外键是 ON DELETE CASCADE——真删下去，
      这个人所有参与记录会被数据库**静默**带走，报表直接少一块，
      而且没有任何报错。归档只打一个 deleted_at，
      v_member_public 视图（WHERE deleted_at IS NULL）自动把他过滤掉，
      历史数据一条不少。

    已经归档的人再删一次 → 404：不想让「删两次」看起来像做了什么。
    """
    member = session.get(models.Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    member.deleted_at = func.now()
    session.commit()
    return {"ok": True, "id": member_id, "archived": True}


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


@router.delete("/series/{series_id}", summary="删除活动系列")
def delete_series(
    series_id: int,
    force: bool = Query(False, description="确认要把系列下的场次解绑才传 true"),
    session: Session = Depends(db.get_db),
):
    """删一个活动系列。

    ⚠️ 系列下面是场次，而 activity.series_id 的外键是 ON DELETE SET NULL：
       删系列**不会**删场次，但会把它们从系列上**解绑**——之后按系列查就查不到了
       （「惟学沙龙」下有几场，会突然变成 0 场）。这也是静默的，所以要拦一道：
       默认回 409 说清有几场会被解绑，调用方确认了再带 force=true。

    解绑是显式做的，不指望数据库的 SET NULL——本机 SQLite 的外键默认不生效，
    靠级联会变成「MySQL 上对、本地测试悄悄不对」。
    """
    series = session.get(models.ActivitySeries, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="活动系列不存在")

    children = session.scalars(
        select(models.Activity).where(models.Activity.series_id == series_id)
    ).all()
    if children and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"这个系列下面还有 {len(children)} 场活动，删掉会把它们从系列上解绑"
                "（活动还在，但按系列查不到了）。确认要删加 force=true"
            ),
        )

    for child in children:
        child.series_id = None
    session.delete(series)
    session.commit()
    return {
        "ok": True,
        "id": series_id,
        "deleted": True,
        "detached_activities": len(children),
    }


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


@router.delete("/activities/{activity_id}", summary="删除活动场次")
def delete_activity(
    activity_id: int,
    force: bool = Query(False, description="确认要连同这场活动的参与记录一起删才传 true"),
    session: Session = Depends(db.get_db),
):
    """删一场活动。

    ⚠️ 这场活动下面可能挂着参与记录，而 participation 的外键是 ON DELETE CASCADE：
       不拦的话，删一场活动会**连带删掉一堆人的分工记录**，同样不报错。
       所以默认回 409 说清有几条，调用方确认了再带 force=true。

    参与记录显式删一遍（不指望 CASCADE）：本机 SQLite 的外键默认不生效，
    靠数据库级联会变成「MySQL 上对、本地测试悄悄不对」。
    """
    activity = session.get(models.Activity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="活动不存在")

    records = session.scalars(
        select(models.Participation).where(models.Participation.activity_id == activity_id)
    ).all()
    if records and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"这场活动下面还有 {len(records)} 条参与记录，删掉会一起没。"
                "确认要删加 force=true"
            ),
        )

    for record in records:
        session.delete(record)
    session.delete(activity)
    session.commit()
    return {
        "ok": True,
        "id": activity_id,
        "deleted": True,
        "removed_participations": len(records),
    }


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
    # ⚠️ 已归档（软删除）的成员也要当成「不存在」：
    #    他已经从名单里消失了，还给这个人登记新分工，几乎一定是拿了个过期的 id。
    #    这里放过去的话，会静默写出一条「查不到人」的分工记录。
    member = session.get(models.Member, payload.member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(
            status_code=400,
            detail=f"member_id={payload.member_id} 不存在（也可能是已归档的成员）",
        )

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


@router.patch(
    "/participations/{participation_id}",
    response_model=schemas.ParticipationOut,
    summary="修改参与记录",
)
def update_participation(
    participation_id: int,
    payload: ParticipationUpdate,
    session: Session = Depends(db.get_db),
):
    """改参与记录的分工 / 贡献 / 工时 / 荣誉 / 关联经验。

    ⚠️ 改 role 时要**重新判一次重**：同一个人在这次活动里的另一个角色如果正好也叫
       这个名字，改完就撞上唯一键 (activity_id, member_id, role) 了。
       先查再改，回 409 和一句人话，别让它变成 500。
    """
    record = session.get(models.Participation, participation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="参与记录不存在")

    changes = payload.model_dump(exclude_unset=True)

    new_role = changes.get("role")
    if new_role and new_role != record.role:
        taken = session.scalars(
            select(models.Participation).where(
                models.Participation.activity_id == record.activity_id,
                models.Participation.member_id == record.member_id,
                models.Participation.role == new_role,
                models.Participation.id != participation_id,
            )
        ).first()
        if taken is not None:
            raise HTTPException(
                status_code=409,
                detail=f"他在这场活动里已经是「{new_role}」了，改过去会和那条记录重复",
            )

    for field, value in changes.items():
        setattr(record, field, value)
    session.commit()
    session.refresh(record)
    return record


@router.delete("/participations/{participation_id}", summary="删除参与记录")
def delete_participation(participation_id: int, session: Session = Depends(db.get_db)):
    """删一条参与记录。登错了删掉重记是最常见的操作。

    这条记录没有被任何表引用（经验那边是通过 exp_ids 字符串软关联的，
    删掉之后那边不会报错，只是指不到东西了），所以直接删，不需要 force。
    """
    record = session.get(models.Participation, participation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="参与记录不存在")
    session.delete(record)
    session.commit()
    return {"ok": True, "id": participation_id, "deleted": True}


# ══ 职务与换届（需求文档 FR-4）═══════════════════════════════════
#
# 任务拆解.md 里这块**没派给谁**，但：
#   · 需求文档 §6.5 有 member_position 这张表，FR-4 要求「记录每届职务分配，
#     标记现任；换届时批量更新 is_current」；
#   · 交接文档 §2.1 的「部门成员表（姓名/岗位）」对的就是它，
#     而 K6 录本学年成员时要录岗位（任务拆解 §4.10）。
# 先放在 K4 里；owner 觉得该归别的线，整段挪走即可。
#
# ⚠️ 表名是 member_position，不是 position（POSITION 是 MySQL 关键字）。


@router.get("/member-positions", response_model=list[PositionOut], summary="职务列表")
def list_positions(
    member_id: int | None = Query(None, description="只看某个人任过什么职"),
    term: str = Query("", description="按任期学年筛，如 2026-2027"),
    is_current: bool | None = Query(None, description="只看现任"),
    session: Session = Depends(db.get_db),
):
    stmt = select(models.MemberPosition)
    if member_id is not None:
        stmt = stmt.where(models.MemberPosition.member_id == member_id)
    if term:
        stmt = stmt.where(models.MemberPosition.term == term)
    if is_current is not None:
        stmt = stmt.where(models.MemberPosition.is_current == int(is_current))
    return session.scalars(
        stmt.order_by(models.MemberPosition.term.desc(), models.MemberPosition.id)
    ).all()


@router.post("/member-positions", status_code=201, summary="登记职务")
def create_position(payload: PositionCreate, session: Session = Depends(db.get_db)):
    """记「某人 + 某届 + 某职务」。一人多职务（部长 + 组长）就记两条。"""
    if session.get(models.Member, payload.member_id) is None:
        raise HTTPException(status_code=400, detail=f"member_id={payload.member_id} 不存在")

    data = payload.model_dump()
    data["is_current"] = int(data["is_current"])
    position = models.MemberPosition(**data)
    session.add(position)
    session.commit()
    session.refresh(position)
    return {"ok": True, "id": position.id}


@router.post(
    "/member-positions/rollover",
    response_model=RolloverOut,
    summary="换届：批量改现任",
)
def rollover_positions(payload: RolloverIn, session: Session = Depends(db.get_db)):
    """换届：把 `term` 这一届标成现任，其余各届全部取消现任。

    对应 FR-4 那句「换届时批量更新 is_current」——挨条 PATCH 太容易漏人，
    漏掉的那条会一直挂着「现任」。

    这个动作只认届、不认人：目标届还没有记录时返回的 current 是 0，
    调用方看到 0 就知道该先把新一届的职务录进来。

    ★ 路由必须写在 `/member-positions/{position_id}` 前面：
      否则 `rollover` 会被当成 position_id 去解析，直接回一个 422。
    """
    rows = session.scalars(select(models.MemberPosition)).all()
    current = 0
    for row in rows:
        row.is_current = 1 if row.term == payload.term else 0
        current += row.is_current
    session.commit()
    return RolloverOut(term=payload.term, current=current, cleared=len(rows) - current)


@router.patch(
    "/member-positions/{position_id}",
    response_model=PositionOut,
    summary="修改职务",
)
def update_position(
    position_id: int,
    payload: PositionUpdate,
    session: Session = Depends(db.get_db),
):
    position = session.get(models.MemberPosition, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="职务记录不存在")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_current") is not None:
        changes["is_current"] = int(changes["is_current"])
    for field, value in changes.items():
        setattr(position, field, value)
    session.commit()
    session.refresh(position)
    return position


@router.delete("/member-positions/{position_id}", summary="删除职务记录")
def delete_position(position_id: int, session: Session = Depends(db.get_db)):
    """删一条职务记录（只是这一届的任职记录，成员本身不受影响）。"""
    position = session.get(models.MemberPosition, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="职务记录不存在")
    session.delete(position)
    session.commit()
    return {"ok": True, "id": position_id, "deleted": True}


# ══ 隐私授权（需求文档 FR-6 / §5.1）════════════════════════════
#
# 验收标准第 5 条：「非本部门成员入库前需同意隐私声明，状态可查」。
# 这块任务拆解里也没派给谁，但验收标准点名要它，就先补在这里。
#
# ⚠️ 这里只负责**记录**同意/撤回，不做拦截：需求文档「待确认项 6：敏感数据
#    入库的授权方式」还没定，硬拦会卡住正常录入。等那条定了再决定要不要强制。


@router.get("/privacy-consents", response_model=list[ConsentOut], summary="授权记录列表")
def list_consents(
    member_id: int | None = Query(None, description="看某个人签过什么"),
    consented: bool | None = Query(None, description="只看同意 / 只看已撤回"),
    session: Session = Depends(db.get_db),
):
    stmt = select(models.PrivacyConsent)
    if member_id is not None:
        stmt = stmt.where(models.PrivacyConsent.member_id == member_id)
    if consented is not None:
        stmt = stmt.where(models.PrivacyConsent.consented == int(consented))
    return session.scalars(stmt.order_by(models.PrivacyConsent.id)).all()


@router.post("/privacy-consents", status_code=201, summary="登记同意 / 不同意")
def create_consent(payload: ConsentCreate, session: Session = Depends(db.get_db)):
    """记一条同意（或不同意）。

    ★ 时间戳自动补：§5.1 要求记录「声明版本号、同意时间」，而一条 consented=1
      却没有时间的记录，事后说不清是什么时候立的。
    ★ 顺序：`privacy_consent.member_id` 是外键，所以**先把人录成成员**
      （老师/嘉宾/外院志愿者也先录成 member，member_type 填 外部/老师/嘉宾），
      再来记这条同意。
    """
    if session.get(models.Member, payload.member_id) is None:
        raise HTTPException(status_code=400, detail=f"member_id={payload.member_id} 不存在")

    consent = models.PrivacyConsent(
        member_id=payload.member_id,
        consented=int(payload.consented),
        scope=payload.scope,
        policy_version=payload.policy_version,
        consented_at=func.now() if payload.consented else None,
        revoked_at=None if payload.consented else func.now(),
    )
    session.add(consent)
    session.commit()
    session.refresh(consent)
    return {"ok": True, "id": consent.id}


@router.patch(
    "/privacy-consents/{consent_id}",
    response_model=ConsentOut,
    summary="改授权状态（撤回同意）",
)
def update_consent(
    consent_id: int,
    payload: ConsentUpdate,
    session: Session = Depends(db.get_db),
):
    """`{"consented": false}` 就是撤回同意，撤回时间自动补上。

    ★ 重新同意时把 revoked_at 清掉：一条记录上同时留着「同意时间」和「撤回时间」，
      看数据的人分不清哪个才是当前状态。
    ★ 撤回之后按 §5.1 可以删除或匿名化他的记录——那要动真实数据，不自动做，
      得有人明确地说一声（删授权记录用 DELETE，删人用归档接口）。
    """
    consent = session.get(models.PrivacyConsent, consent_id)
    if consent is None:
        raise HTTPException(status_code=404, detail="授权记录不存在")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("consented") is not None:
        if changes["consented"]:
            consent.consented = 1
            consent.consented_at = consent.consented_at or func.now()
            consent.revoked_at = None
        else:
            consent.consented = 0
            consent.revoked_at = func.now()
    if "scope" in changes:
        consent.scope = changes["scope"]
    if "policy_version" in changes:
        consent.policy_version = changes["policy_version"]
    session.commit()
    session.refresh(consent)
    return consent


@router.delete("/privacy-consents/{consent_id}", summary="删除授权记录")
def delete_consent(consent_id: int, session: Session = Depends(db.get_db)):
    """删一条授权记录（比如这个人已经不在库里了）。

    ⚠️ 这是物理删除，删完「他到底签过没有」就查不出来了。
       只是撤回同意的话用 PATCH——记录留着，可追溯。
    """
    consent = session.get(models.PrivacyConsent, consent_id)
    if consent is None:
        raise HTTPException(status_code=404, detail="授权记录不存在")
    session.delete(consent)
    session.commit()
    return {"ok": True, "id": consent_id, "deleted": True}
