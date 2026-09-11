"""报表与轮值线（K5）的验收测试。

对应的验收标准（任务拆解.md 4.2）：

    能产出按人聚合的工作量统计表。

下面几个用例盯的都是**最容易悄悄算错**的地方。报表的特点是自己不会报错——
数字少了一个人、或者多算了一倍，页面上照样显示得好好的，
只有把预期写死成断言才拦得住。
"""


def test_by_member_includes_people_with_zero(seeded):
    """★ 一次都没参与过的人也必须在报表里，显示 0 次。

    这是全项目最容易写错的一条 SQL：写成 INNER JOIN 的话，
    没有参与记录的人会直接从结果里消失。
    而「谁还没干过」恰恰是安排轮值时最需要看的信息——
    少了人，表面看报表是"正常"的，只是数字不对，很难发现。

    demo 数据里刻意留了 2 个人（测试员庚、测试员辛）零参与。
    """
    resp = seeded.get("/api/reports/by-member")
    assert resp.status_code == 200

    rows = resp.json()
    names = {r["name"] for r in rows}

    assert "测试员庚" in names, "零参与的人从报表里消失了——多半是 JOIN 写成了 INNER"
    assert "测试员辛" in names

    zero = [r for r in rows if r["total"] == 0]
    assert len(zero) == 2, f"预期 2 个人零参与，实际 {len(zero)} 个"


def test_by_member_totals_match_known_demo_data(seeded):
    """总量要对得上 seed 里造的 15 条参与记录。

    数字写死是故意的：这个用例的价值就在于"我记得应该是多少"。
    改了种子数据就要同步改这里，改的时候自然会想一遍
    "我是不是把哪条删了"。
    """
    rows = seeded.get("/api/reports/by-member").json()
    by_name = {r["name"]: r for r in rows}

    # seed 里的分布：甲 4，乙 3，丙/丁/戊/己 各 2，庚/辛 各 0，合计 15
    assert sum(r["total"] for r in rows) == 15, "参与记录总数不对"
    assert by_name["测试员甲"]["total"] == 4
    assert by_name["测试员乙"]["total"] == 3
    assert by_name["测试员丙"]["total"] == 2
    assert by_name["测试员己"]["total"] == 2


def test_role_distribution_is_not_inflated(seeded):
    """★ 角色分布不能因为 JOIN 而重复计数。

    角色是多值维度，如果把它和荣誉一起拼进同一条 SQL，
    会形成笛卡尔积——一个人有 2 个角色、1 次荣誉，就会被算成 2 次。
    这里逐个人核对：各角色次数之和应等于参与总次数。
    """
    rows = seeded.get("/api/reports/by-member").json()

    for row in rows:
        role_sum = sum(row["roles"].values())
        assert role_sum == row["total"], (
            f"{row['name']}：各角色次数加起来是 {role_sum}，"
            f"但参与总次数是 {row['total']}——角色分布被重复计数了"
        )


def test_honors_counted_once_per_record(seeded):
    """荣誉次数不该超过参与总次数。同样的笛卡尔积问题，换个角度卡一遍。"""
    rows = seeded.get("/api/reports/by-member").json()
    for row in rows:
        assert row["honors"] <= row["total"], (
            f"{row['name']}：荣誉 {row['honors']} 次 > 参与 {row['total']} 次"
        )

    # demo 数据里测试员甲 1 次、测试员乙 1 次
    by_name = {r["name"]: r for r in rows}
    assert by_name["测试员甲"]["honors"] == 1
    assert by_name["测试员乙"]["honors"] == 1


def test_never_participated(seeded):
    """「谁还没参与过」——安排轮值时先看这个。"""
    resp = seeded.get("/api/reports/never-participated")
    assert resp.status_code == 200

    rows = resp.json()
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"测试员庚", "测试员辛"}

    # 这个接口查的是 v_member_public 视图，所以同样不能有敏感字段
    forbidden = {"student_no", "phone", "email", "qq", "wechat"}
    for row in rows:
        assert not (forbidden & set(row)), "「谁还没参与过」泄露了敏感字段"


def test_rotation_per_capita(seeded):
    """按学期的轮值统计：人均场次 = 参与记录数 / 参与人数。"""
    resp = seeded.get("/api/reports/rotation")
    assert resp.status_code == 200

    rows = resp.json()
    assert rows, "一条统计都没有"

    terms = {r["term"] for r in rows}
    assert "2025-2026-1" in terms
    assert "2025-2026-2" in terms

    for row in rows:
        if row["members"]:
            expected = round(row["records"] / row["members"], 1)
            assert row["per_capita"] == expected, f"{row['term']} 人均算错了"
        else:
            assert row["per_capita"] == 0


def test_member_history_includes_linked_experience(seeded):
    """履历里要带上关联的经验 id——这是双库关联在报表侧的表现。"""
    rows = seeded.get("/api/members/1/history").json()
    assert rows, "测试员甲应该有参与记录"

    activities = {r["activity_name"] for r in rows}
    assert any("惟学沙龙第1期" in name for name in activities)

    with_exp = [r for r in rows if r["exp_ids"]]
    assert with_exp, "履历里没有任何关联经验，exp_ids 可能没写进去"
    assert all(e.startswith("exp-") for r in with_exp for e in r["exp_ids"])


def test_member_history_unknown_member_returns_404(seeded):
    assert seeded.get("/api/members/999999/history").status_code == 404
