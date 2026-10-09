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
    if created.get("series_id"):
        session.execute(text("UPDATE activity SET series_id = NULL WHERE series_id = :s"),
                        {"s": created["series_id"]})
        session.execute(text("DELETE FROM activity_series WHERE id = :s"),
                        {"s": created["series_id"]})
    if created.get("member_ids"):
        ids = tuple(created["member_ids"])
        # IN 后面跟一个元组，要显式声明 expanding——
        # 不声明的话 SQLAlchemy 会把整个元组当成一个参数，
        # 生成的 SQL 是 `IN (?, ?, ?)` 却只绑一个值，直接报参数数量对不上。
        #
        # 职务、隐私授权也挂在 member_id 上，一并清掉，别留下孤儿行。
        # （MySQL 上这几张表是 ON DELETE CASCADE，删 member 会带走它们；
        #   本机 SQLite 默认不执行外键，所以显式删一遍，两边行为一致。）
        for table in ("participation", "member_position", "privacy_consent", "member"):
            column = "member_id" if table != "member" else "id"
            stmt = text(f"DELETE FROM {table} WHERE {column} IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            session.execute(stmt, {"ids": ids})
    session.commit()


def _new_member(client, name: str, **overrides) -> int:
    """建一个测试用成员。

    `overrides` 用来改默认值（筛选用例要造「同一届/不同状态/不同组」的人）。
    """
    body = {
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
    }
    body.update(overrides)
    resp = client.post("/api/members", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _new_series(client, name: str) -> int:
    resp = client.post("/api/series", json={"name": name, "category": "测试用类别"})
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


# ── 成员筛选（需求文档 FR-1 / 验收标准第 2 条）─────────────────

def test_member_list_filters_by_term_status_org_skill(seeded, scratch):
    """★ 验收标准第 2 条：「能录入当届成员，按届别/组织/状态查询」。

    FR-1 还多要一个「按技能筛选」。四个筛选条件各造一个只该命中自己的人，
    这样任何一个筛选项写错（比如把 join_term 错写进 status 的条件里）都会红。
    """
    mine = _new_member(
        seeded,
        f"{PREFIX}筛选",
        join_term="2025-2026",
        status="在部",
        org_unit="筛选组",
        skills=["筛选技能"],
    )
    scratch.setdefault("member_ids", []).append(mine)

    other = _new_member(
        seeded,
        f"{PREFIX}陪衬",
        join_term="2024-2025",
        status="退部",
        org_unit="别的组",
        skills=["别的技能"],
    )
    scratch.setdefault("member_ids", []).append(other)

    def hits(**params):
        rows = seeded.get("/api/members", params=params).json()
        return {row["id"] for row in rows}

    assert hits(join_term="2025-2026") == {mine}
    assert hits(status="退部") == {other}
    assert hits(org_unit="筛选组") == {mine}
    assert hits(skill="筛选技能") == {mine}

    # ★ 技能要按完整标签匹配：搜「筛选」不该把「筛选技能」捞出来，
    #   否则标签名互为子串时会互相污染（需求文档里的技能是「编程/AI/摄影」这类词）
    assert hits(skill="筛选") == set()


# ── 改成员 ────────────────────────────────────────────────────

def test_update_member_only_touches_given_fields(seeded, scratch):
    """PATCH 只动传上来的字段。

    ⚠️ 如果实现成「把请求体整体覆盖」，前端只想改个职务，
       就会把它没碰过的手机号、邮箱一起清空——静默的数据损坏，事后查不出来。
    """
    member_id = _new_member(seeded, f"{PREFIX}改", student_no="T900001", phone="13000000001")
    scratch.setdefault("member_ids", []).append(member_id)

    resp = seeded.patch(f"/api/members/{member_id}", json={"title": "组长"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "组长"

    # 没传的字段必须原样还在库里
    from api import db as _db
    with _db.SessionLocal() as s:
        row = s.get(models.Member, member_id)
        assert row.phone == "13000000001"
        assert row.email is None


def test_update_member_returns_desensitized_row(seeded, scratch):
    """改完回的是脱敏视图那一行——哪怕刚改过手机号，响应里也不能出现它。"""
    member_id = _new_member(seeded, f"{PREFIX}改脱敏")
    scratch.setdefault("member_ids", []).append(member_id)

    resp = seeded.patch(f"/api/members/{member_id}", json={"phone": "13000000002"})
    assert resp.status_code == 200
    forbidden = {"student_no", "phone", "email", "qq", "wechat", "birthday", "class_no"}
    assert not (forbidden & set(resp.json()))


def test_update_member_to_taken_student_no_returns_409(seeded, scratch):
    """学号是唯一键。改成别人在用的那个，要回 409 说人话，不是 500。

    而且报错里**不能出现那个学号**——错误信息经常被贴进群里问人。
    """
    first = _new_member(seeded, f"{PREFIX}学号甲", student_no="T900002")
    second = _new_member(seeded, f"{PREFIX}学号乙", student_no="T900003")
    scratch.setdefault("member_ids", []).extend([first, second])

    resp = seeded.patch(f"/api/members/{second}", json={"student_no": "T900002"})
    assert resp.status_code == 409
    assert "T900002" not in resp.text
    assert "占用" in resp.json()["detail"]


# ── 归档成员 ──────────────────────────────────────────────────

def test_archive_member_hides_from_list_but_keeps_history(seeded, scratch):
    """★ 删成员是「归档」，不是物理删除。

    真删下去，participation 的外键 CASCADE 会把他的参与记录一起带走、且不报错，
    报表里「谁干过几场」就少了一块。归档只打 deleted_at：
    名单里看不到他，履历里一条不少。
    """
    member_id = _new_member(seeded, f"{PREFIX}归档")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}归档活动")
    scratch["activity_id"] = activity_id

    seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "主讲",
    })

    resp = seeded.delete(f"/api/members/{member_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True

    # 名单里没有了，详情也 404
    ids = {row["id"] for row in seeded.get("/api/members").json()}
    assert member_id not in ids
    assert seeded.get(f"/api/members/{member_id}").status_code == 404

    # 履历和参与记录都还在（历史不能因为有人退部就少一块）
    history = seeded.get(f"/api/members/{member_id}/history").json()
    assert len(history) == 1
    assert history[0]["role"] == "主讲"
    assert len(seeded.get("/api/participations", params={"member_id": member_id}).json()) == 1

    # 归档过的人不能再归档一次（404），也不能再登记新分工
    assert seeded.delete(f"/api/members/{member_id}").status_code == 404
    again = seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "执行",
    })
    assert again.status_code == 400


# ── 改 / 删参与记录 ────────────────────────────────────────────

def test_update_participation_hours_and_role_conflict(seeded, scratch):
    """改工时、改分工都能用；改成和另一条重复的分工要回 409。"""
    member_id = _new_member(seeded, f"{PREFIX}改分工")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}改分工活动")
    scratch["activity_id"] = activity_id

    first = seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "执行",
    }).json()["id"]
    second = seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "观众",
    }).json()["id"]

    # 改工时
    resp = seeded.patch(f"/api/participations/{first}", json={"hours": 2.5})
    assert resp.status_code == 200, resp.text
    assert float(resp.json()["hours"]) == 2.5

    # 把 second 的角色改成和 first 一样 → 三元组重复，409
    conflict = seeded.patch(f"/api/participations/{second}", json={"role": "执行"})
    assert conflict.status_code == 409
    assert "执行" in conflict.json()["detail"]


def test_delete_participation(seeded, scratch):
    """登错了能删掉重记。删完列表里就没了，再删一次是 404。"""
    member_id = _new_member(seeded, f"{PREFIX}删记录")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}删记录活动")
    scratch["activity_id"] = activity_id

    record_id = seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "志愿者",
    }).json()["id"]

    assert seeded.delete(f"/api/participations/{record_id}").status_code == 200
    assert seeded.get("/api/participations", params={"activity_id": activity_id}).json() == []
    assert seeded.delete(f"/api/participations/{record_id}").status_code == 404


# ── 删活动 / 删系列：会连带动到别人的数据，先拦一道 ──────────

def test_delete_activity_with_records_needs_force(seeded, scratch):
    """⚠️ 删活动会连带删掉它的参与记录（外键 CASCADE），不报错。

    所以默认回 409 说清有几条，调用方确认了（force=true）才真删。
    """
    member_id = _new_member(seeded, f"{PREFIX}删活动")
    scratch.setdefault("member_ids", []).append(member_id)
    activity_id = _new_activity(seeded, f"{PREFIX}删活动场次")
    scratch["activity_id"] = activity_id
    seeded.post("/api/participations", json={
        "activity_id": activity_id, "member_id": member_id, "role": "负责人",
    })

    blocked = seeded.delete(f"/api/activities/{activity_id}")
    assert blocked.status_code == 409
    assert "1 条参与记录" in blocked.json()["detail"]
    # 拦住了就是真没删
    assert len(seeded.get("/api/participations", params={"activity_id": activity_id}).json()) == 1

    forced = seeded.delete(f"/api/activities/{activity_id}", params={"force": True})
    assert forced.status_code == 200, forced.text
    assert forced.json()["removed_participations"] == 1
    assert seeded.get("/api/participations", params={"activity_id": activity_id}).json() == []
    scratch.pop("activity_id", None)  # 已经删了，清理阶段不用再删


def test_delete_activity_without_records_is_direct(seeded, scratch):
    activity_id = _new_activity(seeded, f"{PREFIX}空活动")
    scratch["activity_id"] = activity_id
    assert seeded.delete(f"/api/activities/{activity_id}").status_code == 200
    scratch.pop("activity_id", None)
    assert seeded.delete(f"/api/activities/{activity_id}").status_code == 404


def test_delete_series_detaches_activities_only_with_force(seeded, scratch):
    """⚠️ 删系列不删场次，但会把场次从系列上解绑（之后按系列查就查不到）——同样先拦一道。"""
    series_id = _new_series(seeded, f"{PREFIX}系列")
    scratch["series_id"] = series_id

    activity_id = seeded.post("/api/activities", json={
        "name": f"{PREFIX}系列下属场次",
        "series_id": series_id,
        "term": "2025-2026-1",
        "event_date": "2025-10-02",
        "location": "测试用地点",
        "status": "计划",
    }).json()["id"]
    scratch["activity_id"] = activity_id

    blocked = seeded.delete(f"/api/series/{series_id}")
    assert blocked.status_code == 409
    assert "1 场活动" in blocked.json()["detail"]

    forced = seeded.delete(f"/api/series/{series_id}", params={"force": True})
    assert forced.status_code == 200, forced.text
    assert forced.json()["detached_activities"] == 1
    scratch.pop("series_id", None)

    # 活动还在，只是没有系列了
    rows = seeded.get("/api/activities").json()
    mine = [r for r in rows if r["id"] == activity_id]
    assert len(mine) == 1
    assert mine[0]["series_id"] is None


# ── 职务与换届（需求文档 FR-4）────────────────────────────────

def test_member_positions_and_rollover(seeded, scratch):
    """FR-4：记每届职务、标记现任；换届时批量改 is_current。"""
    member_id = _new_member(seeded, f"{PREFIX}职务")
    scratch.setdefault("member_ids", []).append(member_id)

    first = seeded.post("/api/member-positions", json={
        "member_id": member_id, "title": "组长",
        "org_unit": "知识库组", "term": "2025-2026",
    })
    assert first.status_code == 201, first.text

    second = seeded.post("/api/member-positions", json={
        "member_id": member_id, "title": "部长", "term": "2026-2027", "is_current": False,
    })
    assert second.status_code == 201, second.text

    # 一人多职务：两条都在；默认是现任的只有第一条
    listed = seeded.get("/api/member-positions", params={"member_id": member_id}).json()
    assert {row["title"] for row in listed} == {"组长", "部长"}
    current = seeded.get("/api/member-positions", params={"is_current": True}).json()
    assert [row["title"] for row in current] == ["组长"]

    # 换届：2026-2027 变成现任，2025-2026 自动取消
    rolled = seeded.post("/api/member-positions/rollover", json={"term": "2026-2027"})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["term"] == "2026-2027"
    assert rolled.json()["current"] >= 1
    current = seeded.get("/api/member-positions", params={"is_current": True}).json()
    assert [row["title"] for row in current] == ["部长"]

    # 改一条、删一条
    position_id = listed[0]["id"]
    patched = seeded.patch(f"/api/member-positions/{position_id}", json={"org_unit": "技术组"})
    assert patched.status_code == 200
    assert patched.json()["org_unit"] == "技术组"
    assert seeded.delete(f"/api/member-positions/{position_id}").status_code == 200
    assert seeded.patch(f"/api/member-positions/{position_id}", json={"title": "组员"}).status_code == 404


def test_create_position_with_unknown_member_fails_readably(seeded):
    resp = seeded.post("/api/member-positions", json={
        "member_id": 999999, "title": "组员", "term": "2025-2026",
    })
    assert resp.status_code == 400
    assert "999999" in resp.json()["detail"]


# ── 隐私授权（需求文档 FR-6 / 验收标准第 5 条）────────────────

def test_privacy_consent_record_query_and_revoke(seeded, scratch):
    """★ 验收标准第 5 条：「非本部门成员入库前需同意隐私声明，状态可查」。

    §5.1 要求记「声明版本号、同意时间」，而且**可撤回**。
    """
    guest_id = _new_member(seeded, f"{PREFIX}嘉宾", member_type="嘉宾", join_term="2025-2026")
    scratch.setdefault("member_ids", []).append(guest_id)

    created = seeded.post("/api/privacy-consents", json={
        "member_id": guest_id, "scope": "入库", "policy_version": "v1",
    })
    assert created.status_code == 201, created.text
    consent_id = created.json()["id"]

    row = seeded.get("/api/privacy-consents", params={"member_id": guest_id}).json()[0]
    assert row["consented"] is True
    assert row["consented_at"] is not None  # 同意时间自动补上
    assert row["revoked_at"] is None
    assert row["policy_version"] == "v1"

    # 撤回：状态变 false，撤回时间补上
    revoked = seeded.patch(f"/api/privacy-consents/{consent_id}", json={"consented": False})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["consented"] is False
    assert revoked.json()["revoked_at"] is not None
    assert seeded.get("/api/privacy-consents", params={"consented": False}).json()

    # 重新同意：撤回时间清掉，不留「又同意又撤回」这种自相矛盾的记录
    regranted = seeded.patch(f"/api/privacy-consents/{consent_id}", json={"consented": True})
    assert regranted.status_code == 200
    assert regranted.json()["revoked_at"] is None

    # 删掉就查不到了
    assert seeded.delete(f"/api/privacy-consents/{consent_id}").status_code == 200
    assert seeded.get("/api/privacy-consents", params={"member_id": guest_id}).json() == []


def test_create_consent_with_unknown_member_fails_readably(seeded):
    resp = seeded.post("/api/privacy-consents", json={"member_id": 999999})
    assert resp.status_code == 400
    assert "999999" in resp.json()["detail"]


def test_consent_never_leaks_sensitive_member_fields(seeded, scratch):
    """授权记录的响应里只有 member_id，不会有手机号/学号那一串。"""
    guest_id = _new_member(seeded, f"{PREFIX}嘉宾脱敏", student_no="T900009", phone="13000000009")
    scratch.setdefault("member_ids", []).append(guest_id)
    seeded.post("/api/privacy-consents", json={"member_id": guest_id})

    forbidden = {"student_no", "phone", "email", "qq", "wechat", "birthday", "class_no"}
    for row in seeded.get("/api/privacy-consents", params={"member_id": guest_id}).json():
        assert not (forbidden & set(row))
