"""中间格式约定 —— 「数据拉取」与「清洗入库」之间的接口。

这是本线两个人**唯一需要事先谈拢**的东西，也是最先要做的事。

    拉取（fetch.py）───> 本文件定义的格式 ───> 清洗入库（clean.py）

谈拢之后两人可并行：拉取的只管产出这个格式，清洗的只管消费这个格式，
谁也不用等谁。没谈拢之前，清洗方只能干等——这是本线唯一的阻塞点。

── 格式长什么样 ──────────────────────────────────────────────

两种记录，各一张表，各自一个 CSV 文件：

  1. 成员记录   member.csv        —— 成员表（姓名/岗位/性别/专业/联系方式）
  2. 参与记录   participation.csv —— 项目台账（谁在哪个项目里干了什么）

列名一律用**英文小写下划线**，不用中文列名。原因：
中文列名在不同系统间转手时容易因编码不一致变成乱码，
而且写代码时得反复切换输入法，容易多一个空格就查不出数据。

文件编码固定 **utf-8-sig**。带 BOM 是为了让 Excel 双击打开时
中文不乱码；Python 读的时候 `encoding="utf-8-sig"` 会自动吃掉 BOM。

── 两条硬规则（来自需求文档与任务拆解，不是可选项） ───────────────

1. **被丢弃的行必须记录。** 任何一行因为格式不对、找不到对应成员、
   数值离谱而没入库，都必须写进 `data/rejects/`，带上行号和原因。
   静默丢数据比丢数据本身更严重——后面发现数据少了，根本无从查起。

2. **本线涉及真实数据，一律不得进 Git。** 拉下来的原始文件、
   清洗后的中间文件、丢弃清单，全部落在 `data/` 下，已被 .gitignore 拦住。
   本目录（`pipeline/`）里只放代码，不放任何数据文件。

── 敏感字段 ──────────────────────────────────────────────────

带 `sensitive=True` 的字段属于个人信息（学号、手机、QQ、微信、邮箱）。
这些字段：
  - 只在 `member.csv` 里出现，不进参与记录；
  - 入库后只能被内网服务读取，对外一律走 `v_member_public` 视图；
  - 不要写进任何打印到终端或日志的内容里——`clean.py` 的报错信息
    只提姓名和行号，不回显具体号码。
"""
from dataclasses import dataclass
from datetime import date, datetime

# CSV 读写统一用的编码。utf-8-sig = 带 BOM 的 utf-8，
# Excel 双击打开不乱码，Python 读的时候会自动去掉 BOM。
ENCODING = "utf-8-sig"


@dataclass(frozen=True)
class Field:
    """一个列的约定。"""

    name: str
    type: type
    required: bool = False
    sensitive: bool = False
    note: str = ""


# ── 成员记录 ──────────────────────────────────────────────────
# 对应 member 表。列名与 schema.sql 保持一致，方便对照。
MEMBER_FIELDS = (
    Field("name", str, required=True, note="姓名"),
    Field("student_no", str, sensitive=True, note="学号/工号。唯一，靠它去重"),
    Field("gender", int, note="0未知 1男 2女"),
    Field("grade", str, note="年级，如2026级"),
    Field("major", str, note="专业"),
    Field("join_term", str, note="入部学年，如2026-2027"),
    Field("org_unit", str, note="所属组"),
    Field("title", str, note="部长/副部长/组长/组员"),
    Field("phone", str, sensitive=True, note="手机"),
    Field("qq", str, sensitive=True),
    Field("wechat", str, sensitive=True),
    Field("email", str, sensitive=True),
    Field("skills", str, note="技能标签，多个用竖线分隔，如 Python|SQL"),
    Field("bio_short", str, note="一句话简介（对外可见）"),
)

# ── 参与记录 ──────────────────────────────────────────────────
# 对应 activity + participation 两张表。
#
# 注意这里是**按名字**关联，不是按 id：
# 源表格里写的是人名和活动名，没有 id。把名字换成 id 是 clean.py 的活，
# 换不出来的行就要记进丢弃清单（最常见的就是「台账里的名字和成员表对不上」）。
PARTICIPATION_FIELDS = (
    Field("activity_name", str, required=True, note="活动名。activity.name"),
    Field("activity_series", str, note="所属系列名。对应 activity_series.name"),
    Field("term", str, note="学年学期，如2026-2027-1"),
    Field("event_date", str, note="活动日期，YYYY-MM-DD"),
    Field("location", str, note="地点"),
    Field("activity_status", str, note="计划/进行中/已完成/取消"),
    Field("member_name", str, required=True, note="谁的参与记录。按姓名找 member.id"),
    Field("role", str, required=True, note="分工/角色：负责人/主讲/执行/志愿者/观众"),
    Field("contribution", str, note="做了什么"),
    Field("hours", float, note="投入工时，单位小时"),
    Field("honor", str, note="评优/获奖"),
)

MEMBER_CSV = "member.csv"
PARTICIPATION_CSV = "participation.csv"


# ── 校验 ──────────────────────────────────────────────────────

def _coerce(value, type_, field_name: str):
    """把一个字符串转成约定的类型。转不了就抛 ValueError，带上人话原因。"""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None

    if type_ is str:
        return text

    if type_ is int:
        try:
            return int(float(text))  # 源表格里 1 常常是 "1.0"
        except ValueError:
            raise ValueError(f"{field_name} 应该是整数，实际是「{text}」")

    if type_ is float:
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{field_name} 应该是数字，实际是「{text}」")

    if type_ is date:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"{field_name} 日期格式认不出来：「{text}」，应为 YYYY-MM-DD")

    raise ValueError(f"{field_name} 的类型 {type_} 没有约定转换方式")


def validate(row: dict, fields, line_no: int | None = None):
    """校验一行，返回 (干净的行, 错误原因列表)。

    不抛异常——一行不合格不该让整批数据停下。
    调用方拿到 errors 非空时，把这一行连同行号写进丢弃清单。

    多余列直接忽略：源表格里常有一些备注列、辅助列，
    它们不在约定里，无视即可，不算错误。
    """
    clean = {}
    errors = []

    for field in fields:
        raw = row.get(field.name)
        try:
            value = _coerce(raw, field.type, field.name)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if value is None:
            if field.required:
                # 不再拼「第 N 行」——调用方的丢弃清单里已经有行号列了，
                # 重复一遍反而让原因列看不清楚。
                errors.append(f"必填列 {field.name} 是空的")
            continue

        clean[field.name] = value

    # 性别只认 0/1/2，写「男」「女」直接判错，不猜——
    # 猜错的结果是静默存进去一个错误的性别，比报错难查得多。
    if "gender" in clean and clean["gender"] not in (0, 1, 2):
        errors.append(f"gender 只能是 0/1/2，实际是 {clean['gender']}")

    return clean, errors


def validate_member(row: dict, line_no: int | None = None):
    return validate(row, MEMBER_FIELDS, line_no)


def validate_participation(row: dict, line_no: int | None = None):
    return validate(row, PARTICIPATION_FIELDS, line_no)


def describe(fields=MEMBER_FIELDS) -> str:
    """打印列约定，给人看的。改完约定可以跑一下确认。

    用法：python -m pipeline.contract
    """
    lines = []
    for f in fields:
        marks = []
        if f.required:
            marks.append("必填")
        if f.sensitive:
            marks.append("敏感")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        note = f"  # {f.note}" if f.note else ""
        lines.append(f"  {f.name:<16} {f.type.__name__:<8}{suffix}{note}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"成员记录 {MEMBER_CSV}：")
    print(describe(MEMBER_FIELDS))
    print()
    print(f"参与记录 {PARTICIPATION_CSV}：")
    print(describe(PARTICIPATION_FIELDS))
    print()
    print(f"编码：{ENCODING}")
