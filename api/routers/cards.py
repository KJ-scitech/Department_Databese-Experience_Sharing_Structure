"""介绍卡：K6。

把一个人 / 一场活动的信息拼成一张**能直接复制走**的卡片
（贴进群公告、活动总结、新部员手册）。

这个文件只有三条红线要守：

1. **只查 `v_member_public` 视图，绝不查 `member` 表。**
   学号 / 手机 / 邮箱 / 班级 / 生日这些列**根本不在视图里**，
   所以「忘了脱敏」在这里不是靠自觉避免的，是**做不到**。
2. **「做过什么」走 `v_member_history` 视图**，不自己 JOIN member × activity。
   自己 JOIN 等于绕开视图、把四表连接再写一遍；以后视图改了，这份对不上。
3. **一行 HTML 都不产出。** 内容是接口的事，排版是前端的事。
   接口吐 HTML 的话，换个前端（QQ 机器人、小程序）就得重写一遍。

★ 响应**不套 Pydantic 模型**（`response_model=None`），这是故意的：
  字段清单是从 SQLAlchemy 映射元数据现取的（`inspect(...).columns`），
  手写一个模型就等于把 `v_member_public` 的字段表抄了第二份——
  视图改了、模型没改，字段会**静默少一个**（Pydantic 默认丢弃模型里没有的键）。
  `models.MemberPublic` 的 docstring 明确要求「不要在代码里再抄一份字段清单」，
  这里是同一条规矩。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db, models, schemas

router = APIRouter(prefix="/api/cards", tags=["介绍卡 K6"])


# ── 小工具 ────────────────────────────────────────────────────

def _as_dict(row) -> dict:
    """把映射实例摊成 dict，**字段清单现取**，不手抄。

    用 `inspect(类).columns` 而不是写死 ["id", "name", …]：
    视图以后加/删了字段，代码自动跟着变，永远不会出现
    「视图删了列、代码还在取」或反过来的静默错位。
    """
    return {col.key: getattr(row, col.key) for col in sa_inspect(type(row)).columns}


def _as_list(value) -> list[str]:
    """skills / honors 是 JSON 列，可能是 list、可能是 dict、也可能是 NULL。

    统一成「字符串列表」，让下面拼卡片的地方只需要面对一种形状。
    手填的 JSON 什么样都有可能（`{"1": "优秀部员"}` 这种也不奇怪），
    所以这里不做假设，只保证输出是个干净的列表。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return [str(value)]
    return [str(v) for v in items if v not in (None, "")]


def _join(*parts, sep: str = " · ") -> str:
    """只把「真有值」的部分连起来。

    空字段直接不参与拼接——不写「职务：（）」这种空壳。
    卡片是要贴出去给人看的，空壳比没有更难看。
    """
    return sep.join(str(p) for p in parts if p not in (None, ""))


def _with_paren(main, extra) -> str:
    """「主体（补充）」——补充得有值才加括号，没有就只留主体。

    和 _join 分开是因为分隔符不同：这里补充说明是**贴着**主体的
    （「惟学沙龙（学业支持）」），用 _join 的 " · " 会变成
    「惟学沙龙 · （学业支持）」——括号悬在那儿，读着像掉了个词。
    """
    main = str(main) if main else ""
    return f"{main}（{extra}）" if extra else main


# ── 卡片文字：纯函数，不碰数据库 ───────────────────────────────

def _history_line(item: dict) -> str:
    """履历里的一行。"""
    # 有具体日期就写日期，没有就退回学期——两个都没有就只写活动名。
    when = item.get("event_date") or item.get("term")
    head = _join(when, item.get("activity_name"), sep=" ")
    if item.get("role"):
        head += f"（{item['role']}）"

    tails = []
    if item.get("contribution"):
        tails.append(str(item["contribution"]))
    if item.get("honor"):
        tails.append(f"获{item['honor']}")
    return "· " + head + (f" —— {'；'.join(tails)}" if tails else "")


def member_card_text(member: dict, history: list[dict]) -> str:
    """拼一张成员卡的文字版。

    ★ 纯函数：输入是两个 dict，不碰数据库，可以直接单测。
      （放这里而不是塞在路由里，就是为了让 tests/test_cards.py 能秒级跑完，
      不用等 embedding 模型加载 20 秒。）
    """
    lines = [f"【{member.get('name') or '（未填姓名）'}】"]

    line = _join(member.get("grade"), member.get("major"))
    if line:
        lines.append(line)

    line = _join(member.get("org_unit"), member.get("title"))
    if line:
        lines.append(line)

    line = _join(
        member.get("status"),
        f"{member['join_term']} 入部" if member.get("join_term") else None,
    )
    if line:
        lines.append(f"状态：{line}")

    skills = _as_list(member.get("skills"))
    if skills:
        lines.append(f"技能：{'、'.join(skills)}")

    honors = _as_list(member.get("honors"))
    if honors:
        lines.append(f"荣誉：{'、'.join(honors)}")

    if member.get("bio_short"):
        lines.append(str(member["bio_short"]))

    if history:
        lines.append(f"参与活动（{len(history)} 项）：")
        lines.extend("  " + _history_line(item) for item in history)
    else:
        # 明确写「暂无」而不是留空：留空看着像接口漏了字段。
        lines.append("参与活动：暂无记录")

    if member.get("avatar"):
        lines.append(f"头像：{member['avatar']}")

    return "\n".join(lines)


def activity_card_text(activity: dict, participants: list[dict]) -> str:
    """拼一张活动卡的文字版。同样是纯函数。"""
    lines = [f"【{activity.get('name') or '（未命名活动）'}】"]

    line = _with_paren(activity.get("series_name"), activity.get("category"))
    if line:
        lines.append(f"系列：{line}")

    line = _with_paren(activity.get("event_date"), activity.get("term"))
    if line:
        lines.append(f"时间：{line}")

    if activity.get("location"):
        lines.append(f"地点：{activity['location']}")
    if activity.get("status"):
        lines.append(f"状态：{activity['status']}")
    if activity.get("description"):
        lines.append(str(activity["description"]))
    if activity.get("article_url"):
        lines.append(f"报道：{activity['article_url']}")

    if participants:
        # 写「人次」不写「人」：participation 的唯一键是 (活动, 人, 角色)，
        # 同一个人在一个活动里可以有多个角色，写「2 人」会和下面两行对不上。
        lines.append(f"参与（{len(participants)} 人次）：")
        for item in participants:
            # 活动卡里不需要 _history_line：每行都已经知道自己是谁的活动，
            # 「· 时间 活动名（角色）」太啰嗦。换成「· 人名（角色）—— 贡献」，
            # 读起来才是「谁干了什么」。
            who = item.get("member_name") or "（未知成员）"
            role = f"（{item['role']}）" if item.get("role") else ""
            tail = f" —— {item['contribution']}" if item.get("contribution") else ""
            honor = f"，获{item['honor']}" if item.get("honor") else ""
            lines.append(f"  · {who}{role}{tail}{honor}")
    else:
        lines.append("参与：暂无记录")

    return "\n".join(lines)


# ── 取数据 ────────────────────────────────────────────────────

def _member_card(member, history: list[dict]) -> dict:
    fields = _as_dict(member)
    return {
        "member_id": fields["id"],
        "fields": fields,
        "activities": history,
        "card_text": member_card_text(fields, history),
    }


def _history_of(session: Session, member_id: int | None = None, activity_id: int | None = None) -> list:
    """履历行。**过滤条件交给数据库**，不拉回来在 Python 里筛。

    ★ ORDER BY 必须凑成**全序**，最后两个键 (member_id, role) 不能省。
      participation 的唯一键是 (活动, 人, 角色)：同一个人在同一场活动里
      可以有多个角色，于是这些行的 event_date 和 activity_id **完全相同**，
      排序键到那儿就分不出先后了。MySQL 对并列行的返回顺序不保证稳定，
      同一个人的履历，从「列所有卡」和「取单张卡」两条 SQL 里出来可能是**两个顺序**
      ——卡片文字也就跟着飘，而这两条路本该给出同一张卡。

      （这个 bug 是 test_single_member_card_matches_the_list 抓出来的：
      它断言单人卡和列表里的那张必须逐字节相同。）
    """
    stmt = select(models.MemberHistory).order_by(
        models.MemberHistory.event_date,
        models.MemberHistory.activity_id,
        models.MemberHistory.member_id,
        models.MemberHistory.role,
    )
    if member_id is not None:
        stmt = stmt.where(models.MemberHistory.member_id == member_id)
    if activity_id is not None:
        stmt = stmt.where(models.MemberHistory.activity_id == activity_id)
    return list(session.scalars(stmt))


def _visible_member_ids(session: Session) -> set[int]:
    """v_member_public 里出现过的 id —— 也就是「没被软删」的人。

    ★ 为什么需要这个：`v_member_history` 的 SQL 直接 JOIN `member`，**没有**
      `deleted_at IS NULL` 这个条件（见 schema/schema.sql:185）。
      也就是说软删掉的人，他的历史参与记录仍然会从履历视图里冒出来。
      而成员卡走的是 v_member_public，软删的人根本取不到卡——
      两边一对比，活动卡上就会挂着一个点不开的人名。

      这里不改视图（视图是共享的，改它会影响 K4/K5 两条线），
      改成在活动卡这边按「视图里存在的人」过一遍。
      TODO 真要根治，应当在 schema 里给 v_member_history 也加 deleted_at 条件，
      那要动共享文件，等 K4 的 PR 合完再说。
    """
    return set(session.scalars(select(models.MemberPublic.id)))


# ── 接口 ──────────────────────────────────────────────────────

@router.get("/members", response_model=None, summary="成员介绍卡（在部）")
def list_member_cards(session: Session = Depends(db.get_db)):
    """在部成员一人一张卡。

    只取 `status == "在部"` 的：做介绍卡是为了对外展示当下的人，
    退部的还在列表里会让人以为名单没更新。单人卡接口（下面那个）
    不过滤状态，所以退部成员的卡仍然取得到——那是做归档用的。
    """
    members = session.scalars(
        select(models.MemberPublic)
        .where(models.MemberPublic.status == "在部")
        .order_by(models.MemberPublic.id)
    ).all()

    # 展示顺序：职务高的在前（主席 → 部长 → 副部长 → 组长 → 部员），同职务按姓名。
    # 在 Python 里排而不是写进 SQL：顺序是**展示规则**，跟数据库无关；
    # 而且这样和前端、报表用的是同一个 schemas.title_rank，不会各排各的。
    members = sorted(members, key=lambda m: (schemas.title_rank(m.title), m.name or ""))

    history_by_member: dict[int, list[dict]] = {}
    for row in _history_of(session):
        history_by_member.setdefault(row.member_id, []).append(_as_dict(row))

    return [_member_card(m, history_by_member.get(m.id, [])) for m in members]


@router.get("/members/{member_id}", response_model=None, summary="单张成员卡")
def get_member_card(member_id: int, session: Session = Depends(db.get_db)):
    """一张成员卡。退部的也取得到（归档用），软删的取不到。"""
    member = session.get(models.MemberPublic, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail=f"成员 {member_id} 不存在（或已删除）")

    history = [_as_dict(row) for row in _history_of(session, member_id=member_id)]
    return _member_card(member, history)


@router.get("/activities", response_model=None, summary="活动介绍卡")
def list_activity_cards(session: Session = Depends(db.get_db)):
    """每场活动一张卡，附参与名单。

    ★ 用 LEFT JOIN 连系列：`activity.series_id` 可以为空（一次性活动），
      INNER JOIN 会让这类活动**整场消失**，而不是少一个系列名。
      少信息可以接受，少一场活动不行。
    """
    rows = session.execute(
        select(models.Activity, models.ActivitySeries.name, models.ActivitySeries.category)
        .join(models.ActivitySeries, models.ActivitySeries.id == models.Activity.series_id, isouter=True)
        .order_by(models.Activity.event_date, models.Activity.id)
    ).all()

    visible = _visible_member_ids(session)
    participants_by_activity: dict[int, list[dict]] = {}
    for row in _history_of(session):
        if row.member_id not in visible:
            continue  # 软删成员的历史记录，见 _visible_member_ids 的说明
        participants_by_activity.setdefault(row.activity_id, []).append(_as_dict(row))

    return [
        _activity_card(activity, series_name, category, participants_by_activity.get(activity.id, []))
        for activity, series_name, category in rows
    ]


@router.get("/activities/{activity_id}", response_model=None, summary="单张活动卡")
def get_activity_card(activity_id: int, session: Session = Depends(db.get_db)):
    row = session.execute(
        select(models.Activity, models.ActivitySeries.name, models.ActivitySeries.category)
        .join(models.ActivitySeries, models.ActivitySeries.id == models.Activity.series_id, isouter=True)
        .where(models.Activity.id == activity_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"活动 {activity_id} 不存在")

    activity, series_name, category = row
    visible = _visible_member_ids(session)
    participants = [
        _as_dict(item)
        for item in _history_of(session, activity_id=activity_id)
        if item.member_id in visible
    ]
    return _activity_card(activity, series_name, category, participants)


def _activity_card(activity, series_name: str | None, category: str | None, participants: list[dict]) -> dict:
    fields = _as_dict(activity)
    fields["series_name"] = series_name
    fields["category"] = category
    return {
        "activity_id": fields["id"],
        "fields": fields,
        "participants": participants,
        "card_text": activity_card_text(fields, participants),
    }
