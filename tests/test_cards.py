"""介绍卡（K6）的验收测试。

分两段：
  上半段是**纯函数**测试（拼卡片文字），不碰数据库、不加载模型，秒级跑完；
  下半段要 `seeded`（会加载模型，跟着大部队一起等）。

上半段之所以能不用夹具，是因为拼卡片这件事被刻意抽成了纯函数——
`member_card_text(member, history)` 只吃两个 dict。
排版逻辑最容易出「空壳」「None 字样」这类小毛病，
把它和数据库解开，才能用最便宜的方式钉住。
"""
from datetime import date

from sqlalchemy import func, select

from api import models
from api.routers.cards import _as_list, activity_card_text, member_card_text


# ── 上半段：拼卡片文字（纯函数） ───────────────────────────────

def test_member_card_text_degrades_gracefully():
    """★ 只有一个名字时也不能出现空壳。

    「职务：（）」「技能：None」「状态： · 」这类东西不会报错，
    但它是**要贴到群里**的，空壳比没有更难看。
    """
    text = member_card_text({"name": "某人"}, [])

    assert text == "【某人】\n参与活动：暂无记录"
    assert "（）" not in text
    assert "None" not in text


def test_member_card_text_renders_every_filled_field():
    text = member_card_text(
        {
            "name": "某人",
            "grade": "2025级",
            "major": "软件工程",
            "org_unit": "知识库组",
            "title": "部员",
            "status": "在部",
            "join_term": "2025-2026",
            "skills": ["Python"],
            "honors": ["优秀部员"],
            "bio_short": "负责数据管道",
            "avatar": "https://example.invalid/a.png",
        },
        [
            {
                "event_date": date(2025, 11, 14),
                "activity_name": "惟学沙龙第1期：期末复习方法",
                "role": "主讲",
                "contribution": "讲稿与答疑",
                "honor": "优秀主讲",
            }
        ],
    )

    assert "【某人】" in text
    assert "2025级 · 软件工程" in text
    assert "知识库组 · 部员" in text
    assert "状态：在部 · 2025-2026 入部" in text
    assert "技能：Python" in text
    assert "荣誉：优秀部员" in text
    assert "负责数据管道" in text
    assert "参与活动（1 项）：" in text
    assert "  · 2025-11-14 惟学沙龙第1期：期末复习方法（主讲） —— 讲稿与答疑；获优秀主讲" in text


def test_history_line_falls_back_to_term_when_no_date():
    """没填具体日期的活动（还在「计划」里）要退回学期，不能只剩活动名。"""
    text = member_card_text({}, [{"term": "2026-2027-1", "activity_name": "新生线上答疑第二场"}])

    assert "· 2026-2027-1 新生线上答疑第二场" in text


def test_activity_card_text_lists_participants():
    text = activity_card_text(
        {
            "name": "惟学沙龙第1期：期末复习方法",
            "series_name": "惟学沙龙",
            "category": "学业支持",
            "event_date": date(2025, 11, 14),
            "term": "2025-2026-1",
            "location": "仙林校区 逸夫楼 B203",
            "status": "已完成",
        },
        [{"member_name": "某人", "role": "主讲", "contribution": "讲稿", "honor": "优秀主讲"}],
    )

    assert "【惟学沙龙第1期：期末复习方法】" in text
    assert "系列：惟学沙龙（学业支持）" in text
    assert "时间：2025-11-14（2025-2026-1）" in text
    assert "地点：仙林校区 逸夫楼 B203" in text
    assert "状态：已完成" in text
    assert "参与（1 人次）：" in text
    assert "  · 某人（主讲） —— 讲稿，获优秀主讲" in text


def test_activity_card_text_says_none_when_nobody_joined():
    text = activity_card_text({"name": "还没人报名的活动"}, [])

    assert text == "【还没人报名的活动】\n参与：暂无记录"


def test_activity_card_counts_person_times_not_people():
    """★ 计数单位是「人次」不是「人」。

    participation 的唯一键是 (活动, 人, 角色)——同一个人在同一场活动里
    可以有多个角色（既当主讲又当执行）。按人去重的话，
    卡片上写「2 人」却列出 3 行，自己跟自己都对不上。
    """
    text = activity_card_text(
        {"name": "动员会"},
        [
            {"member_name": "某人", "role": "主讲"},
            {"member_name": "某人", "role": "执行"},
        ],
    )

    assert "参与（2 人次）：" in text
    assert text.count("· 某人") == 2


def test_as_list_handles_every_shape():
    """skills / honors 是 JSON 列，手填成什么样都有可能。"""
    assert _as_list(None) == []
    assert _as_list("") == []
    assert _as_list("Python") == ["Python"]
    assert _as_list(["Python", None, ""]) == ["Python"]
    assert _as_list({"1": "Python", "2": "SQL"}) == ["Python", "SQL"]


# ── 下半段：接口 ───────────────────────────────────────────────

def test_every_active_member_has_a_card(seeded, session):
    """★ K6 的完成标准：在部成员一人一张卡，且卡片文字非空。

    期望值现查数据库，不写死数字——种子里加了人，这条测试不该红。
    """
    active = session.scalar(
        select(func.count())
        .select_from(models.Member)
        .where(models.Member.status == "在部", models.Member.deleted_at.is_(None))
    )
    assert active, "库里一个在部成员都没有，先跑 python -m scripts.seed"

    cards = seeded.get("/api/cards/members").json()

    assert len(cards) == active, f"在部 {active} 人，却只有 {len(cards)} 张卡"
    for card in cards:
        assert card["card_text"].strip(), f"member {card['member_id']} 的卡片文字是空的"


def test_card_fields_come_from_the_view_not_a_handwritten_list(seeded):
    """★ 字段清单必须是**现取**的，和 v_member_public 的列一一对应。

    这条测试守的是「不要在代码里再抄一份字段清单」这条规矩：
    视图加了一列、卡片接口没跟上（或者反过来），这里立刻红。
    """
    from sqlalchemy import inspect as sa_inspect

    expected = {col.key for col in sa_inspect(models.MemberPublic).columns}
    card = seeded.get("/api/cards/members").json()[0]

    assert set(card["fields"]) == expected


def test_member_card_never_leaks_sensitive_fields(seeded, session):
    """★★ K6 最重要的一条：介绍卡里不能出现学号 / 手机 / 邮箱。

    这条**真的**造一个带敏感字段的成员，再从卡片接口往回找这些值——
    断言的是「接口吐出来的原始文本里没有它」，比检查字段名更硬：
    就算将来有人把敏感列塞进别的字段名里，也一样会被抓到。

    ★ 只查 v_member_public 是这个红线的落点：敏感列压根不在视图里。
      哪天有人把 cards.py 里的 MemberPublic 换成 Member，这条立刻红。
    """
    student_no, phone, email = "2024999901", "13900000901", "leak@example.invalid"

    created = seeded.post("/api/members", json={
        "name": "脱敏回归临时成员",
        "status": "在部",
        "student_no": student_no,
        "phone": phone,
        "email": email,
    })
    assert created.status_code == 201, created.text
    member_id = created.json()["id"]

    try:
        resp = seeded.get("/api/cards/members")
        assert resp.status_code == 200

        for secret in (student_no, phone, email):
            assert secret not in resp.text, f"{secret} 出现在介绍卡里了——K6 的脱敏红线破了"

        card = next(c for c in resp.json() if c["member_id"] == member_id)
        for key in ("student_no", "phone", "email", "birthday", "class_name"):
            assert key not in card["fields"], f"{key} 不该出现在介绍卡的字段里"
    finally:
        # 删除走数据库而不是接口：K4 的删除接口要权限，在 PR #5 里，还没合进来。
        # 硬删这里安全——这个临时成员没有任何参与记录，不会碰到外键。
        member = session.get(models.Member, member_id)
        if member is not None:
            session.delete(member)
            session.commit()


def test_activity_cards_have_text_and_participants(seeded):
    cards = seeded.get("/api/cards/activities").json()
    assert cards, "一场活动都没有，先跑 python -m scripts.seed"

    for card in cards:
        assert card["card_text"].strip(), f"activity {card['activity_id']} 的卡片文字是空的"

    with_people = [c for c in cards if c["participants"]]
    assert with_people, "种子里好几场活动都有人参与，一张带名单的卡都没有，多半是 JOIN 断了"

    joined = [c for c in with_people if c["fields"]["series_name"]]
    assert joined, "系列名全空——LEFT JOIN activity_series 大概退化成空连接了"


def test_activity_participants_match_the_history_view(seeded, session):
    """活动卡的名单要和 v_member_history 对得上，不能多也不能少。"""
    card = next(c for c in seeded.get("/api/cards/activities").json() if c["participants"])

    expected = session.scalar(
        select(func.count())
        .select_from(models.MemberHistory)
        .where(models.MemberHistory.activity_id == card["activity_id"])
    )
    assert len(card["participants"]) == expected


def test_single_member_card_matches_the_list(seeded):
    """单人卡和列表里的那张必须是同一张——两条取数路径不能各拼各的。"""
    listed = seeded.get("/api/cards/members").json()[0]

    one = seeded.get(f"/api/cards/members/{listed['member_id']}")
    assert one.status_code == 200
    assert one.json() == listed


def test_single_activity_card_matches_the_list(seeded):
    listed = seeded.get("/api/cards/activities").json()[0]

    one = seeded.get(f"/api/cards/activities/{listed['activity_id']}")
    assert one.status_code == 200
    assert one.json() == listed


def test_missing_cards_return_404(seeded):
    assert seeded.get("/api/cards/members/999999").status_code == 404
    assert seeded.get("/api/cards/activities/999999").status_code == 404


def test_介绍卡按职务排序(seeded):
    """介绍卡：职务高的在前，同职务按姓名。

    只断言权重单调不减，不写死具体是谁——CI 跑的是种子数据，
    里面没有主席/部长，写死名字会让这条测试变成「数据一变就红」。
    """
    from api import schemas

    cards = seeded.get("/api/cards/members").json()
    ranks = [schemas.title_rank(c["fields"].get("title")) for c in cards]

    assert ranks == sorted(ranks), "介绍卡没按职务排序"
