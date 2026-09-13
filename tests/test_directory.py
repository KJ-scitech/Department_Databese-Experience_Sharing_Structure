"""分工数据库线（K4）的验收测试。

对应的验收标准（任务拆解.md 4.1）：

    能通过接口完整记录一条「某成员在某活动中承担某分工」。

以及 4.2 里那条硬要求——**查询一律走 v_member_public 视图**。
这条规则很容易在后续开发中被绕开，所以下面专门有用例钉住它。

这些用例会真的往库里写数据，但每一条都在结束时删干净，
所以不会影响别的用例。用「测试夹具-」前缀是为了万一手工看库时
一眼能认出哪些是测试留下的。
"""
import pytest
from sqlalchemy import bindparam, text

from api import models

PREFIX = "测试夹具-"


@pytest.fixture
def scratch(seeded, session):
    """造一套一次性的测试数据，跑完删干净。

    清理顺序按外键来：先删参与记录，再删活动，最后删成员。
    虽然 schema 上 activity / member 都是 ON DELETE CASCADE，
    删活动会自动带走它的参与记录，但显式删一遍更稳妥——
    万一以后有人把外键改了，这里不会留下一堆孤儿行。
    """
    created: dict = {}
    yield created

    if created.get("activity_id"):
        session.execute(text("DELETE FROM participation WHERE activity_id = :a"),
                        {"a": created["activity_id"]})
        session.execute(text("DELETE FROM activity WHERE id = :a"),
                        {"a": created["activity_id"]})
    if created.get("member_ids"):
        ids = tuple(created["member_ids"])
        # IN 后面跟一个元组，要显式声明 expanding——
        # 不声明的话 SQLAlchemy 会把整个元组当成一个参数，
        # 生成的 SQL 是 `IN (?, ?, ?)` 却只绑一个值，直接报参数数量对不上。
        for table in ("participation", "member"):
            column = "member_id" if table == "participation" else "id"
            stmt = text(f"DELETE FROM {table} WHERE {column} IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            session.execute(stmt, {"ids": ids})
    session.commit()


def _new_member(client, name: str) -> int:
    resp = client.post("/api/members", json={
        "name": name,
        "gender": 1,
        "grade": "2025级",
        "major": "计算机科学与技术",
        "org_unit": "知识库组",
        "title": "组员",
        "skills": ["Python"],
        "student_no": None,
        "phone": None,
        "email": None,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _new_activity(client, name: str) -> int:
    resp = client.post("/api/activities", json={
        "name": name,
        "term": "2025-2026-1",
        "event_date": "2025-10-01",
        "location": "测试用地点",
        "status": "计划",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── 成员 ──────────────────────────────────────────────────────

def test_create_member_then_read_back(seeded, scratch):
    member_id = _new_member(seeded, f"{PREFIX}甲")
    scratch.setdefault("member_ids", []).append(member_id)

    resp = seeded.get(f"/api/members/{member_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == f"{PREFIX}甲"
    assert body["skills"] == ["Python"]


def test_member_endpoints_never_leak_sensitive_fields(seeded, scratch):
    """★ 4.2 的硬要求：对外查询不得出现学号/手机/邮箱。

    这是全项目最容易「改着改着就漏了」的一条规则：
    顺手在 schemas.MemberOut 里加一个字段，敏感信息就出去了，
    而且接口照常工作、测试也不会报错——除非有人专门盯着。

    所以这里不检查某一个接口，而是检查返回的**每一个键**。
    """
    member_id = _new_member(seeded, f"{PREFIX}乙")
    scratch.setdefault("member_ids", []).append(member_id)

    # 先直接往库里塞一份敏感信息，确保这些字段确实有值——
    # 空值的话就算接口漏了也看不出来。
    from api import db as _db
    with _db.SessionLocal() as s:
        row = s.get(models.Member, member_id)
        row.student_no = "T0000001"
        row.phone = "13000000000"
        row.email = "test@example.invalid"
        s.commit()

    forbidden = {"student_no", "phone", "email", "qq", "wechat", "birthday", "class_no"}

    for url in ("/api/members", f"/api/members/{member_id}"):
        resp = seeded.get(url)
        assert resp.status_code == 200
        payload = resp.json()
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            leaked = forbidden & set(row)
            assert not leaked, f"{url} 泄露了敏感字段：{leaked}"


def test_member_not_found(seeded):
    assert seeded.get("/api/members/999999").status_code == 404


def test_member_keyword_search(seeded, scratch):
    member_id = _new_member(seeded, f"{PREFIX}丙")
    scratch.setdefault("member_ids", []).append(member_id)

    resp = seeded.get("/api/members", params={"keyword": f"{PREFIX}丙"})
    hits = resp.json()
    assert len(hits) == 1
    assert hits[0]["id"] == member_id


# ── 参与记录（本线的核心验收） ────────────────────────────────

def test_register_a_full_participation(seeded, scratch):
    """★ 验收标准：完整记录一条「某成员在某活动中承担某分工」。"""
    member_id = _new_member(seeded, f"{PREFIX}丁")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}活动")
    scratch["activity_id"] = activity_id

    resp = seeded.post("/api/participations", json={
        "activity_id": activity_id,
        "member_id": member_id,
        "role": "负责人",
        "contribution": "测试用，跑完就删",
        "hours": 3.5,
        "honor": "测试荣誉",
    })
    assert resp.status_code == 201, resp.text

    listed = seeded.get("/api/participations", params={"activity_id": activity_id})
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["member_id"] == member_id
    assert rows[0]["role"] == "负责人"
    assert float(rows[0]["hours"]) == 3.5


def test_same_member_can_hold_two_roles_in_one_activity(seeded, scratch):
    """★ 唯一键是三元组 (activity_id, member_id, role)，不是二元组。

    同一个人在同一场活动里既当主讲又当执行，是两条记录，
    都该被接受。如果哪天有人把唯一键理解成「一个人一场活动只能一条」，
    这个用例会红。
    """
    member_id = _new_member(seeded, f"{PREFIX}戊")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}活动双角色")
    scratch["activity_id"] = activity_id

    for role in ("负责人", "主讲"):
        resp = seeded.post("/api/participations", json={
            "activity_id": activity_id, "member_id": member_id, "role": role,
        })
        assert resp.status_code == 201, f"{role} 应该能登记成功：{resp.text}"

    rows = seeded.get("/api/participations", params={"activity_id": activity_id}).json()
    assert {r["role"] for r in rows} == {"负责人", "主讲"}


def test_duplicate_participation_returns_409_with_readable_message(seeded, scratch):
    """完全相同的三元组重复登记，要返回 409 和一句能看懂的话。

    不拦的话会撞唯一键、抛 IntegrityError，前端只会看到 500。
    """
    member_id = _new_member(seeded, f"{PREFIX}己")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}活动重复")
    scratch["activity_id"] = activity_id

    payload = {"activity_id": activity_id, "member_id": member_id, "role": "志愿者"}
    assert seeded.post("/api/participations", json=payload).status_code == 201

    again = seeded.post("/api/participations", json=payload)
    assert again.status_code == 409
    assert "已经是" in again.json()["detail"]


def test_participation_appears_in_member_history(seeded, scratch):
    """登记完要能在个人履历里查出来——这条链断了，报表就没意义了。"""
    member_id = _new_member(seeded, f"{PREFIX}庚")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}活动履历")
    scratch["activity_id"] = activity_id

    seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "执行",
    })

    resp = seeded.get(f"/api/members/{member_id}/history")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["activity_id"] == activity_id
    assert rows[0]["role"] == "执行"
    assert rows[0]["member_name"] == f"{PREFIX}庚"


def test_participation_with_unknown_foreign_key_fails_readably(seeded, scratch):
    """登记一个不存在的 activity_id / member_id，要返回 400 加一句能看懂的话。

    不加这层校验的话，INSERT 会直接撞上 fk_part_activity / fk_part_member，
    SQLAlchemy 抛 IntegrityError 没人接，接口回 500「Internal Server Error」。

    对调用方来说这是最没用的一种回复。尤其是 QQ 机器人：它从群消息里
    拼出一个 activity_id，传错了自己也不知道，只能拿到一句英文的
    「Internal Server Error」，既不知道错在哪，也没法告诉群里的人。

    400 加一句「activity_id=999 不存在」，机器人才有办法回「没有这场活动」。

    （这个校验是 experience.py 里对 author_id / activity_id 一直在做的，
      本条用例把 participation 拉齐到同一套做法。）
    """
    member_id = _new_member(seeded, f"{PREFIX}外键")
    scratch.setdefault("member_ids", []).append(member_id)

    # 活动不存在
    bad_activity = seeded.post(
        "/api/participations",
        json={"activity_id": 999999, "member_id": member_id, "role": "志愿者"},
    )
    assert bad_activity.status_code == 400
    assert "999999" in bad_activity.json()["detail"]

    # 人不存在（这场活动是真的，见上面新建）
    activity_id = _new_activity(seeded, f"{PREFIX}活动外键")
    scratch["activity_id"] = activity_id

    bad_member = seeded.post(
        "/api/participations",
        json={"activity_id": activity_id, "member_id": 999999, "role": "志愿者"},
    )
    assert bad_member.status_code == 400
    assert "999999" in bad_member.json()["detail"]
