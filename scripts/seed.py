"""造演示数据。

运行：python -m scripts.seed

前提：表已经建好了（python -m scripts.init_db 或 reset_db）。

注意：这里的人名全是编造的（测试员甲、测试员乙…）。
   真实姓名一律不进仓库——这是交付仓库的红线。
   要跑真实数据，用 pipeline/ 那套，别改这个文件。

幂等：可以反复跑，每次先清空再重建。

这个脚本原先在 api/seed.py，挪到 scripts/ 是因为它属于「运维脚本」
而不是「Web 层」。api/ 里只留对外提供接口的代码。
"""
import sys
from datetime import date

from sqlalchemy import text

from api import db, models
from api import vectors as vec
from scripts import console


# ── 成员：8 人 ────────────────────────────────────────────────
# 刻意留 2 个人（测试员庚、测试员辛）一条参与记录都没有，
# 好让「谁还没参与过」这条报表有输出。
MEMBERS = [
    dict(name="测试员甲", gender=1, grade="2025级", major="计算机科学与技术", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组长", skills=["Python", "数据库"],
         bio_short="负责经验检索线"),
    dict(name="测试员乙", gender=2, grade="2025级", major="软件工程", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组员", skills=["Python"],
         bio_short="负责经验检索线"),
    dict(name="测试员丙", gender=1, grade="2025级", major="人工智能", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组员", skills=["SQL", "MySQL"],
         bio_short="负责分工数据库写入"),
    dict(name="测试员丁", gender=2, grade="2025级", major="信息管理", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组员", skills=["SQL"],
         bio_short="负责分工数据库读取"),
    dict(name="测试员戊", gender=1, grade="2025级", major="计算机科学与技术", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组员", skills=["数据清洗"],
         bio_short="负责数据管道"),
    dict(name="测试员己", gender=2, grade="2025级", major="电子信息", join_term="2025-2026",
         status="在部", org_unit="知识库组", title="组员", skills=["Excel", "Python"],
         bio_short="负责数据管道"),
    dict(name="测试员庚", gender=1, grade="2026级", major="计算机科学与技术", join_term="2026-2027",
         status="在部", org_unit="知识库组", title="组员", skills=["前端"],
         bio_short="本学期新入部"),
    dict(name="测试员辛", gender=2, grade="2026级", major="软件工程", join_term="2026-2027",
         status="在部", org_unit="知识库组", title="组员", skills=["前端"],
         bio_short="本学期新入部"),
]

# ── 活动系列：3 个 ────────────────────────────────────────────
SERIES = [
    dict(name="惟学沙龙", category="学业支持", description="面向全院的学习经验分享", is_official=1),
    dict(name="新生答疑", category="师生交流", description="开学季线上答疑", is_official=1),
    dict(name="竞赛宣讲", category="竞赛赛事", description="各类学科竞赛动员与宣讲", is_official=0),
]

# ── 活动场次：6 场，跨两个学期 ────────────────────────────────
# series 填的是 SERIES 里的下标，落库时换成真实 id
ACTIVITIES = [
    dict(series=0, name="惟学沙龙第1期：期末复习方法", term="2025-2026-1",
         event_date=date(2025, 11, 14), location="仙林校区 逸夫楼 B203", status="已完成"),
    dict(series=0, name="惟学沙龙第2期：科研入门", term="2025-2026-1",
         event_date=date(2025, 12, 5), location="仙林校区 逸夫楼 B203", status="已完成"),
    dict(series=1, name="新生线上答疑第一场", term="2025-2026-2",
         event_date=date(2026, 3, 8), location="腾讯会议", status="已完成"),
    dict(series=2, name="程序设计竞赛动员会", term="2025-2026-2",
         event_date=date(2026, 3, 22), location="仙林校区 逸夫楼 A101", status="已完成"),
    dict(series=0, name="惟学沙龙第3期：海外交换分享", term="2025-2026-2",
         event_date=date(2026, 4, 18), location="仙林校区 逸夫楼 B203", status="已完成"),
    dict(series=1, name="新生线上答疑第二场", term="2026-2027-1",
         event_date=date(2026, 9, 6), location="腾讯会议", status="计划"),
]

# ── 参与记录 ──────────────────────────────────────────────────
# (成员下标, 活动下标, 角色, 做了什么, 工时, 荣誉)
# 注意同一人同一活动可以有多个角色——唯一键是三元组，不是二元组
PARTICIPATIONS = [
    (0, 0, "负责人", "统筹整场，定主题、约主讲人", 6.0, "优秀组织者"),
    (0, 0, "主讲", "自己讲了一半的复习方法", 2.0, None),
    (1, 0, "执行", "做海报、借教室、现场签到", 4.0, None),
    (2, 0, "志愿者", "现场引导与设备调试", 3.0, None),
    (0, 1, "负责人", "联系老师，确定时间", 5.0, None),
    (3, 1, "执行", "推文撰写与发布", 4.5, None),
    (4, 1, "志愿者", "场地布置", 2.5, None),
    (1, 2, "负责人", "主持线上答疑，整理问题清单", 4.0, "优秀组织者"),
    (5, 2, "执行", "会议记录", 3.0, None),
    (2, 3, "负责人", "对接竞赛教练", 4.5, None),
    (3, 3, "主讲", "讲自己的参赛经历", 1.5, None),
    (4, 3, "志愿者", "报名统计", 2.0, None),
    (0, 4, "执行", "联系交换回来的学长", 3.5, None),
    (5, 4, "志愿者", "摄影", 2.0, None),
    (1, 4, "观众", "到场参与", 1.0, None),
]

# ── 经验条目：8 条 ────────────────────────────────────────────
# (正文, 类型, 标签, 作者下标, 活动下标)
#
# 第 1 条是给语义检索演示用的：查询「设备出故障了咋办」应该召回它，
# 但按关键词搜「设备」一条都搜不到——两者没有共同词。
EXPERIENCES = [
    ("活动现场投影仪突然不亮，先别慌着拆线。九成是信号源没切对，按遥控器上的 Source 键切到 HDMI2 就好了。"
     "备用方案是提前把 PPT 存一份到 U 盘，教室电脑里一般也有 Office。",
     "解答", ["设备", "投影仪", "应急预案"], 0, 0),

    ("借教室要提前三天在系统里申请，当天去基本没位置。逸夫楼的 B203 有投影和音响，A101 只有投影。",
     "笔记", ["场地", "申请流程"], 1, 0),

    ("做推文排版时，字号别小于 14px，行距 1.75 最舒服。学校官方推文的配色可以直接抄，省事又不出错。",
     "笔记", ["推文", "排版"], 3, 1),

    ("约主讲人一定要提前两周，老师们的日程很满。约的时候把「时间、地点、主题、听众大概多少人」一次说清，"
     "不要问「老师您什么时候有空」，那样来回要问好几轮。",
     "笔记", ["约人", "沟通"], 0, 1),

    ("线上答疑最容易冷场。开场的十分钟先准备好三个必问的问题自己抛出来，气氛起来了后面就自然有人问了。",
     "解答", ["线上活动", "答疑"], 1, 2),

    ("腾讯会议记得开录制，会后自动转文字，整理纪要能省一半时间。但录制前一定要在群里说一声正在录制。",
     "笔记", ["腾讯会议", "会议记录"], 5, 2),

    ("报名的统计表格建议一开始就用在线表格，不要用微信群接龙。接龙到最后几十条的时候根本数不清，还得重来。",
     "牢骚", ["报名", "统计"], 4, 3),

    ("上次搬桌子把腰闪了，后来学乖了：重的东西一定找人一起抬，别自己硬来。场地布置至少留一个小时。",
     "牢骚", ["场地", "安全"], 4, 3),
]


def _clear(session):
    """按外键顺序清空所有表。

    顺序不能乱：participation 引用了 activity 和 member，
    先删引用方（子表）再删被引用方（父表），否则外键会拦住。
    """
    for table in (
        "participation",
        "member_position",
        "privacy_consent",
        "account",
        "activity",
        "activity_series",
        "member",
    ):
        session.execute(text(f"DELETE FROM `{table}`"))
        session.execute(text(f"ALTER TABLE `{table}` AUTO_INCREMENT = 1"))
    session.commit()


def _check_schema(session) -> bool:
    """先确认表建好了，不然会报一堆看不懂的错。

    返回 False 表示没建好，调用方别接着往下跑。
    """
    rows = session.execute(text("SHOW FULL TABLES")).all()
    names = {row[0] for row in rows}
    required = {"member", "activity", "activity_series", "participation",
                "member_position", "privacy_consent", "account"}
    missing = required - names
    if missing:
        print(f"失败：库 {db.engine.url.database} 里缺表 {', '.join(sorted(missing))}")
        print("   先建库建表：在项目根目录跑  python -m scripts.init_db")
        return False
    if "v_member_public" not in names:
        print("失败：缺视图 v_member_public，schema 没建全。")
        print("   跑 python -m scripts.reset_db 重建。")
        return False
    return True


def run() -> int:
    console.setup()

    with db.SessionLocal() as session:
        if not _check_schema(session):
            return 1

        print("清空旧数据…")
        _clear(session)
        vec.clear()

        # 1. 成员
        members = [models.Member(**row) for row in MEMBERS]
        session.add_all(members)
        session.commit()
        for m in members:
            session.refresh(m)
        print(f"成员 {len(members)} 人")

        # 2. 活动系列
        series_list = [models.ActivitySeries(**row) for row in SERIES]
        session.add_all(series_list)
        session.commit()
        for s in series_list:
            session.refresh(s)
        print(f"活动系列 {len(series_list)} 个")

        # 3. 活动场次
        activities = []
        for row in ACTIVITIES:
            data = dict(row)
            data["series_id"] = series_list[data.pop("series")].id
            activities.append(models.Activity(**data))
        session.add_all(activities)
        session.commit()
        for a in activities:
            session.refresh(a)
        print(f"活动场次 {len(activities)} 场")

        # 4. 参与记录
        participations = []
        for member_idx, activity_idx, role, contribution, hours, honor in PARTICIPATIONS:
            participations.append(
                models.Participation(
                    member_id=members[member_idx].id,
                    activity_id=activities[activity_idx].id,
                    role=role,
                    contribution=contribution,
                    hours=hours,
                    honor=honor,
                    exp_ids=[],
                )
            )
        session.add_all(participations)
        session.commit()
        print(f"参与记录 {len(participations)} 条")

        # 5. 经验：写进向量库（MySQL 里没有 experience 表）
        print("生成向量（要加载模型，约 15–30 秒）…")
        exp_ids = []
        for content, kind, tags, author_idx, activity_idx in EXPERIENCES:
            exp_id = vec.next_id()
            vec.add(
                exp_id,
                content,
                {
                    "type": kind,
                    "tags": tags,
                    "author_id": members[author_idx].id,
                    "activity_id": activities[activity_idx].id,
                    "source": "演示数据",
                },
            )
            exp_ids.append(exp_id)

            # 反向关联：把经验挂到对应的参与记录上
            for p in participations:
                if p.member_id == members[author_idx].id and p.activity_id == activities[activity_idx].id:
                    p.exp_ids = list(p.exp_ids or []) + [exp_id]
                    break
        session.commit()
        print(f"经验 {len(exp_ids)} 条：{', '.join(exp_ids)}")

    print("\n完成：演示数据就绪。")
    print("   启动服务： python -m uvicorn api.main:app --reload --port 8000")
    print("   打开网页： http://localhost:8000")
    print("   试一下检索： http://localhost:8000/api/experience/search?q=设备出故障了咋办")
    return 0


if __name__ == "__main__":
    sys.exit(run())
