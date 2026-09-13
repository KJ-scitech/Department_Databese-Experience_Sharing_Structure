"""数据管道线的验收测试。

对应的验收标准（任务拆解.md 5.2）：

    数据能入库，且重复执行不产生重复记录。

以及 5.2 里那条硬要求：

    被丢弃的数据行必须记录（哪些行、原因），不得静默丢弃。

这两条都不容易靠肉眼看出来——重复数据要跑两遍才知道，
丢数据更是"不报就等于没发生"。所以都写成测试。

本文件不加载 embedding 模型（用不到），跑起来很快。
"""
import pytest
from sqlalchemy import func, select, text

from api import models
from pipeline import clean, contract

PREFIX = "测试夹具-"


# ══ 一、纯函数：清洗与校验 ════════════════════════════════════
# 这部分不碰数据库，跑得飞快，改 contract / clean 的规则时先看这里。

@pytest.mark.parametrize("raw, expected", [
    ("男", 1), ("女", 2), ("未知", 0), ("", 0), ("1", 1), (2, 2),
])
def test_normalize_gender(raw, expected):
    """性别不能只认数字——源表格里人写的是「男」「女」。"""
    assert clean.normalize("member", {"gender": raw})["gender"] == expected


@pytest.mark.parametrize("raw, expected", [
    ("6小时", "6"), ("2.5h", "2.5"), ("3", "3"), ("", None), (None, None),
])
def test_normalize_hours_strips_units(raw, expected):
    """工时不能要求填的人不带单位——「2.5小时」很常见。"""
    assert clean.normalize("participation", {"hours": raw})["hours"] == expected


@pytest.mark.parametrize("raw, expected", [
    ("Python|SQL", ["Python", "SQL"]),
    ("Python、SQL", ["Python", "SQL"]),
    ("Python,SQL", ["Python", "SQL"]),
    ("Python", ["Python"]),
    ("", None), (None, None),
])
def test_normalize_skills(raw, expected):
    """技能列的各种分隔符都要认。落库是 JSON 数组，所以给 list。"""
    assert clean.normalize("member", {"skills": raw})["skills"] == expected


def test_validate_rejects_missing_required():
    """必填列是空的要报出来，不能放过去。"""
    _, errors = contract.validate_member({"name": "", "gender": 1})
    assert errors, "空姓名应该报错"
    assert any("name" in e for e in errors)


def test_validate_rejects_unparseable_gender():
    """「男士」这种认不出来的值要报错，不能猜成男的。

    猜错的后果是静默存进一个错的性别，比报错难查得多。
    """
    _, errors = contract.validate_member({"name": "某人", "gender": "男士"})
    assert errors
    assert any("gender" in e for e in errors)


def test_validate_treats_empty_optional_as_absent():
    """可选列空着不算错，也不该塞个空字符串进去。"""
    row, errors = contract.validate_member({"name": "某人", "major": "  ", "gender": None})
    assert errors == []
    assert "major" not in row
    assert "gender" not in row


def test_validate_ignores_extra_columns():
    """源表格里的备注列、辅助列不在约定里，无视即可，不算错误。"""
    row, errors = contract.validate_member({
        "name": "某人", "备注": "这列不在约定里", "填表人": "谁",
    })
    assert errors == []
    assert "备注" not in row


def test_ambiguous_names_in_same_file_are_detected():
    """★ 同一批文件里出现两次同名、且都没学号 → 必须判为歧义。

    这是最容易出事、也最难发现的一种：
    名册第一次导入时库里还没有任何人，两个同名的人会一个接一个地
    匹配上同一行，第二个人直接改掉第一个人的信息。
    跑完看不出来，事后也没法回溯。
    """
    rows = [
        (2, {"name": "张三", "student_no": ""}),
        (3, {"name": "张三", "student_no": ""}),
        (4, {"name": "李四", "student_no": ""}),
    ]
    assert clean._ambiguous_names(rows) == {"张三"}


def test_names_with_student_no_are_not_ambiguous():
    """带学号的同名不算歧义——学号能区分开。"""
    rows = [
        (2, {"name": "张三", "student_no": "001"}),
        (3, {"name": "张三", "student_no": "002"}),
    ]
    assert clean._ambiguous_names(rows) == set()


# ══ 二、落库：幂等 ════════════════════════════════════════════

@pytest.fixture
def scratch_member(session):
    """造一个带学号的测试成员，跑完删掉。

    用学号而不是姓名来定位，因为姓名可能和演示数据撞车。
    """
    student_no = "T9000001"
    session.execute(text("DELETE FROM member WHERE student_no = :s"), {"s": student_no})
    session.commit()

    yield student_no

    session.execute(text("DELETE FROM member WHERE student_no = :s"), {"s": student_no})
    session.commit()


def test_load_members_is_idempotent(session, tables_ready, scratch_member):
    """★ 验收标准：同一个文件跑两遍，第二遍不产生重复数据。

    第一遍新增，第二遍应该是「更新 1、新增 0」，
    并且库里那个学号只能有一条记录。
    """
    student_no = scratch_member
    rows = [(2, {
        "name": f"{PREFIX}幂等",
        "student_no": student_no,
        "gender": "男",
        "grade": "2025级",
    })]

    first = clean.Report(kind="member")
    clean.load_members(session, rows, first, dry_run=False)
    assert first.rejected == 0, f"不该有丢弃：{first.rejects}"
    assert first.inserted == 1
    assert first.updated == 0

    second = clean.Report(kind="member")
    clean.load_members(session, rows, second, dry_run=False)
    assert second.rejected == 0
    assert second.inserted == 0, "跑第二遍居然又新增了——幂等没做对"
    assert second.updated == 1

    count = session.execute(
        select(func.count()).select_from(models.Member)
        .where(models.Member.student_no == student_no)
    ).scalar()
    assert count == 1, f"库里出现了 {count} 条同号记录，重复了"


def test_load_members_updates_instead_of_duplicating(session, tables_ready, scratch_member):
    """第二遍改了内容，应该更新到原记录上，而不是新插一条。"""
    student_no = scratch_member

    clean.load_members(
        session,
        [(2, {"name": f"{PREFIX}改前", "student_no": student_no, "grade": "2025级"})],
        clean.Report(kind="member"),
        dry_run=False,
    )
    clean.load_members(
        session,
        [(2, {"name": f"{PREFIX}改后", "student_no": student_no, "grade": "2026级"})],
        clean.Report(kind="member"),
        dry_run=False,
    )

    rows = session.execute(
        select(models.Member).where(models.Member.student_no == student_no)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == f"{PREFIX}改后"
    assert rows[0].grade == "2026级"


def test_dry_run_writes_nothing(session, tables_ready):
    """★ --dry-run 必须真的不落库。

    这个选项是用来"先看看会怎样"的，如果它其实会写，
    那就是个陷阱——用户以为安全，实际已经改了库。
    """
    student_no = "T9000002"
    session.execute(text("DELETE FROM member WHERE student_no = :s"), {"s": student_no})
    session.commit()

    report = clean.Report(kind="member")
    clean.load_members(
        session,
        [(2, {"name": f"{PREFIX}干跑", "student_no": student_no})],
        report,
        dry_run=True,
    )

    assert report.inserted == 1, "dry-run 也该报告「会发生什么」"
    count = session.execute(
        select(func.count()).select_from(models.Member)
        .where(models.Member.student_no == student_no)
    ).scalar()
    assert count == 0, "dry-run 居然真的写库了"


# ══ 三、丢弃必须有记录 ════════════════════════════════════════

def test_rejected_rows_are_recorded_not_silently_dropped():
    """★ 硬要求：丢数据不报，比丢数据本身更严重。

    这里直接验 Report.reject 的行为——每一行被丢弃时，
    都要留下行号、原因、原始内容三样东西。
    """
    report = clean.Report(kind="member")
    raw = {"name": "", "gender": "男士"}

    report.reject(7, "必填列 name 是空的", raw)

    assert report.rejected == 1
    assert len(report.rejects) == 1

    item = report.rejects[0]
    assert item["行号"] == 7
    assert item["原因"] == "必填列 name 是空的"
    assert "男士" in item["原始内容"], "原始内容要留下，不然没法对着原表格查"


def test_rejects_are_written_to_a_file(tmp_path, monkeypatch):
    """丢弃清单要真的落盘，不能只留在内存里。

    落到临时目录，不污染项目里的 data/rejects/。
    """
    monkeypatch.setattr(clean.config, "REJECTS_DIR", tmp_path)

    report = clean.Report(kind="member")
    report.reject(3, "测试原因", {"name": "某人"})

    path = clean.write_rejects(report)
    assert path is not None and path.exists()

    body = path.read_text(encoding=contract.ENCODING)
    assert "行号" in body and "原因" in body
    assert "测试原因" in body


def test_no_rejects_means_no_file(tmp_path, monkeypatch):
    """一行都没丢就不该建文件——免得 data/rejects/ 里全是空文件。"""
    monkeypatch.setattr(clean.config, "REJECTS_DIR", tmp_path)
    assert clean.write_rejects(clean.Report(kind="member")) is None
    assert not list(tmp_path.iterdir())
