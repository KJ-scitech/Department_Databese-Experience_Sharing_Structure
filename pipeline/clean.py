"""清洗与入库：把 data/raw/ 里的数据洗干净，幂等写进 MySQL。

运行：
    python -m pipeline.clean --kind member
    python -m pipeline.clean --kind participation
    python -m pipeline.clean --kind participation --dry-run   # 只看结果不落库

顺序：**成员必须先进库**。参与记录是按姓名去关联 member.id 的，
成员表还是空的时候跑，会把每一行都判成「姓名对不上」丢进丢弃清单。

── 幂等 ─────────────────────────────────────────────────────

同一个文件跑两遍，第二遍不会产生重复数据：
先按主键/唯一键查，查到就更新，查不到才插入。跑完的
「新增 N / 更新 M」两次应该一致（第二次新增为 0）。

── 丢数据必须报出来 ─────────────────────────────────────────

任何一行没能入库，都会写进 data/rejects/<kind>-<时间戳>.csv，
带行号和原因，同时在终端汇总。**不做静默丢弃。**

这份丢弃清单里含真实姓名，跟 data/ 下所有东西一样按敏感数据处理：
只留在本机，不要发群里、不要传网盘。

── 第 4 列开始是「不猜」原则 ──────────────────────────────────

几处看起来可以"猜一下"的地方，这里都选择报错而不是猜：

  · 姓名在成员表里匹配到多个人（同名）  → 记入丢弃清单
  · 活动名在活动表里匹配到多场          → 记入丢弃清单
  · 活动不存在                          → 记入丢弃清单，除非显式加 --create-activities

猜错的后果是数据静默长歪，等发现的时候已经没法回溯哪些是对的。
"""
import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from api import config, db, models
from pipeline import contract
from scripts import console


@dataclass
class Report:
    """一次入库的结果。给测试和终端汇总共用。"""

    kind: str
    read: int = 0
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    rejects: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def reject(self, line_no: int, reason: str, raw: dict) -> None:
        self.rejected += 1
        self.rejects.append({
            "行号": line_no,
            "原因": reason,
            "原始内容": json.dumps(raw, ensure_ascii=False),
        })

    def summary(self) -> str:
        lines = [
            f"读入 {self.read} 行",
            f"新增 {self.inserted} 行，更新 {self.updated} 行",
            f"丢弃 {self.rejected} 行",
        ]
        for key, value in self.extra.items():
            lines.append(f"{key} {value}")
        return "\n".join("   " + line for line in lines)


# ── 清洗规则 ──────────────────────────────────────────────────
# 在 contract 的校验之前跑：先把「人写得随意」的值整成标准写法，
# 再去判断合不合规。分成两步是因为这两件事的错法不一样——
# 「2.5小时」是格式随意（能救），「工时=abc」是数据错了（救不了，该丢）。

_GENDER = {"男": 1, "女": 2, "未知": 0, "": 0, "1": 1, "2": 2, "0": 0}
_SKILL_SEP = "|、,，;；"

# dry-run 时用来占位的哨兵：表示「活动在这一轮里会被新建」。
# 用独立的对象而不是 None，因为 None 已经表示「查不到」。
_PENDING = object()


def _norm_gender(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in _GENDER:
        return _GENDER[text]
    return value  # 认不出来就原样交给 contract 去报错


def _norm_hours(value):
    """「2.5小时」「2.5h」→ 2.5。"""
    if value is None:
        return None
    text = str(value).strip()
    for unit in ("小时", "个小时", "h", "H", "hr", "hours"):
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    return text or None


def _norm_skills(value):
    """「Python|SQL」「Python、SQL」→ ["Python", "SQL"]。

    落库是个 JSON 数组列，SQLAlchemy 收 Python list，
    不需要我们自己 json.dumps。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for sep in _SKILL_SEP:
        text = text.replace(sep, "|")
    items = [part.strip() for part in text.split("|") if part.strip()]
    return items or None


_NORMALIZERS = {
    "gender": _norm_gender,
    "hours": _norm_hours,
    "skills": _norm_skills,
}


def normalize(kind: str, row: dict) -> dict:
    """按规则清洗一行。不判断对错，只做格式规整。"""
    fields = contract.MEMBER_FIELDS if kind == "member" else contract.PARTICIPATION_FIELDS
    out = dict(row)
    for f in fields:
        if f.name in _NORMALIZERS and out.get(f.name) is not None:
            out[f.name] = _NORMALIZERS[f.name](out[f.name])
    return out


# ── 落库 ──────────────────────────────────────────────────────

def _unique(items: list, key):
    """按 key 分组，返回 {key: [匹配到的]}. 用来发现"一对多"的歧义。"""
    grouped: dict = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _ambiguous_names(rows: list[tuple[int, dict]]) -> set[str]:
    """找出「本批文件里出现不止一次、且没给学号」的姓名。

    这种情况不能靠姓名认人。名册第一次导入时，库里还没有任何人，
    两个同名的人会一个接一个地匹配上同一行——第二个人直接改掉
    第一个人的信息，而且事后完全看不出来。
    宁可整批同名行都判为待确认，也不要赌。
    """
    counts: dict = {}
    for _, row in rows:
        name = str(row.get("name") or "").strip()
        student_no = str(row.get("student_no") or "").strip()
        if name and not student_no:
            counts[name] = counts.get(name, 0) + 1
    return {name for name, n in counts.items() if n > 1}


def load_members(session, rows: list[dict], report: Report, dry_run: bool) -> None:
    existing = session.execute(select(models.Member)).scalars().all()
    by_student_no = {m.student_no: m for m in existing if m.student_no}
    by_name = _unique(existing, lambda m: m.name)
    ambiguous = _ambiguous_names(rows)

    for line_no, raw_row in rows:
        # 先清洗格式，再校验。两步分开：清洗只规整写法，校验判对错。
        row = normalize("member", raw_row)
        clean, errors = contract.validate_member(row, line_no)

        if not errors and clean.get("name") in ambiguous and not clean.get("student_no"):
            errors.append(
                f"「{clean['name']}」在本批文件里出现了不止一次，且都没填学号，"
                f"无法区分这两个人。请补上学号"
            )

        if errors:
            # 丢弃清单里记原始行，不是清洗后的——要能对着原表格改。
            report.reject(line_no, "；".join(errors), raw_row)
            continue

        target = None
        # 学号是唯一键，最可靠，优先用它认人
        if clean.get("student_no"):
            target = by_student_no.get(clean["student_no"])
        if target is None:
            same_name = by_name.get(clean["name"], [])
            if len(same_name) > 1:
                # 同名的两个人。不能随便挑一个——挑错了就是把甲的
                # 信息写到乙头上，而且事后完全看不出来。
                report.reject(
                    line_no,
                    f"姓名「{clean['name']}」在成员表里匹配到 {len(same_name)} 个人，"
                    f"无法确定是哪一个。请在源表格里补上学号",
                    raw_row,
                )
                continue
            target = same_name[0] if same_name else None

        if target is None:
            if not dry_run:
                member = models.Member(**clean)
                session.add(member)
                session.flush()
                by_student_no[member.student_no] = member
                by_name.setdefault(member.name, []).append(member)
            report.inserted += 1
        else:
            if not dry_run:
                for key, value in clean.items():
                    setattr(target, key, value)
            report.updated += 1

    if not dry_run:
        session.commit()


def _resolve_activity(session, name: str, cache: dict, create: bool):
    """按名字找活动。返回 (activity, 错误原因)。"""
    if name in cache:
        return cache[name], None

    matches = session.execute(
        select(models.Activity).where(models.Activity.name == name)
    ).scalars().all()

    if len(matches) > 1:
        return None, f"活动名「{name}」匹配到 {len(matches)} 场，无法确定是哪一场"
    if matches:
        cache[name] = matches[0]
        return matches[0], None
    if not create:
        return None, f"活动「{name}」不存在。先建活动，或加 --create-activities 让脚本建"
    return None, None  # 交给调用方新建


def _resolve_or_create_series(session, name: str, cache: dict, report: Report):
    """系列名 → id。不存在就建一个，并记进报告。

    系列数量少且固定，凭空建一个的代价低；但一定要在报告里说出来，
    不能悄悄地建。
    """
    if not name:
        return None
    if name in cache:
        return cache[name]

    found = session.execute(
        select(models.ActivitySeries).where(models.ActivitySeries.name == name)
    ).scalars().first()
    if found:
        cache[name] = found
        return found

    series = models.ActivitySeries(name=name)
    session.add(series)
    session.flush()
    cache[name] = series
    report.extra["新建系列"] = report.extra.get("新建系列", 0) + 1
    return series


def load_participations(session, rows: list[dict], report: Report, dry_run: bool,
                        create_activities: bool) -> None:
    members = session.execute(select(models.Member)).scalars().all()
    if not members:
        print("失败：成员表是空的。先跑 python -m pipeline.clean --kind member")
        raise SystemExit(1)
    by_name = _unique(members, lambda m: m.name)

    activity_cache: dict = {}
    series_cache: dict = {}
    seen_keys: set = set()

    for line_no, raw_row in rows:
        row = normalize("participation", raw_row)
        clean, errors = contract.validate_participation(row, line_no)
        if errors:
            report.reject(line_no, "；".join(errors), raw_row)
            continue

        # 1. 认人
        same_name = by_name.get(clean["member_name"], [])
        if not same_name:
            report.reject(line_no, f"成员表里没有「{clean['member_name']}」这个人", raw_row)
            continue
        if len(same_name) > 1:
            report.reject(
                line_no,
                f"「{clean['member_name']}」有 {len(same_name)} 个同名成员，无法确定是哪一个",
                raw_row,
            )
            continue
        member = same_name[0]

        # 2. 认活动
        activity, err = _resolve_activity(session, clean["activity_name"], activity_cache,
                                          create_activities)
        if err:
            report.reject(line_no, err, raw_row)
            continue
        if activity is _PENDING:
            # 同一个新活动的后续行。这次不是新建了，是往已有活动上挂记录。
            report.updated += 1
            continue
        if activity is None:
            if dry_run:
                # 记一个占位符，免得同一个新活动的后面每一行都重复算成"新增"
                activity_cache[clean["activity_name"]] = _PENDING
                report.inserted += 1
                continue
            series = _resolve_or_create_series(
                session, clean.get("activity_series"), series_cache, report)
            activity = models.Activity(
                name=clean["activity_name"],
                series_id=series.id if series else None,
                term=clean.get("term"),
                event_date=clean.get("event_date"),
                location=clean.get("location"),
                status=clean.get("activity_status") or "计划",
            )
            session.add(activity)
            session.flush()
            activity_cache[clean["activity_name"]] = activity
            report.extra["新建活动"] = report.extra.get("新建活动", 0) + 1

        # 3. 同一批文件里出现两遍同样的三元组 → 后一条覆盖前一条，
        #    但要报出来，因为通常是源表格里重复登记了。
        key = (activity.id, member.id, clean["role"])
        if key in seen_keys:
            report.extra["文件内重复"] = report.extra.get("文件内重复", 0) + 1
        seen_keys.add(key)

        # 4. 幂等的关键：唯一键是 (activity_id, member_id, role)，
        #    按它查，有就更新，没有才插。
        existing = session.execute(
            select(models.Participation).where(
                models.Participation.activity_id == activity.id,
                models.Participation.member_id == member.id,
                models.Participation.role == clean["role"],
            )
        ).scalars().first()

        if existing is None:
            if not dry_run:
                session.add(models.Participation(
                    activity_id=activity.id,
                    member_id=member.id,
                    role=clean["role"],
                    contribution=clean.get("contribution"),
                    hours=clean.get("hours"),
                    honor=clean.get("honor"),
                    # 新记录还没有关联经验。已有关联的记录走下面的更新分支，
                    # 那里刻意不碰 exp_ids——它连的是向量库，不能因为
                    # 重跑一次导入就把已有经验关联抹掉。
                    exp_ids=[],
                ))
            report.inserted += 1
        else:
            if not dry_run:
                if clean.get("contribution") is not None:
                    existing.contribution = clean["contribution"]
                if clean.get("hours") is not None:
                    existing.hours = clean["hours"]
                if clean.get("honor") is not None:
                    existing.honor = clean["honor"]
            report.updated += 1

    if not dry_run:
        session.commit()


# ── 入口 ──────────────────────────────────────────────────────

def read_raw(kind: str, path: Path) -> list[tuple[int, dict]]:
    """读 data/raw/<kind>.csv，返回 [(行号, 行)]。

    行号从 2 起：第 1 行是表头，第 2 行才是第一条数据。
    报错里给的行号要能直接对着 Excel 找，所以按文件的算法来。
    """
    for encoding in (contract.ENCODING, "gbk"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                reader = csv.DictReader(fh)
                return [(i, row) for i, row in enumerate(reader, start=2)]
        except UnicodeDecodeError:
            continue
    print(f"失败：读不出 {path}，utf-8 和 gbk 都试过了。")
    raise SystemExit(1)


def write_rejects(report: Report) -> Path | None:
    """丢弃清单落盘。没有丢弃行就不写文件。"""
    if not report.rejects:
        return None
    config.REJECTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.REJECTS_DIR / f"{report.kind}-{stamp}.csv"
    with path.open("w", encoding=contract.ENCODING, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["行号", "原因", "原始内容"])
        writer.writeheader()
        writer.writerows(report.rejects)
    return path


def run(kind: str, *, path: Path | None = None, dry_run: bool = False,
        create_activities: bool = False) -> Report:
    path = path or (config.RAW_DIR / f"{kind}.csv")
    report = Report(kind=kind)

    if not path.exists():
        print(f"失败：找不到 {path}")
        print(f"   先拉数据：python -m pipeline.fetch --kind {kind}")
        raise SystemExit(1)

    rows = read_raw(kind, path)
    report.read = len(rows)

    print(f"{'（dry-run，不落库）' if dry_run else ''}读入 {path}")
    with db.SessionLocal() as session:
        if kind == "member":
            load_members(session, rows, report, dry_run)
        else:
            load_participations(session, rows, report, dry_run, create_activities)

    print(report.summary())

    reject_path = write_rejects(report)
    if reject_path:
        print(f"\n   被丢弃的行已记录：{reject_path}")
        print("   前几行原因：")
        for item in report.rejects[:5]:
            print(f"     第 {item['行号']} 行：{item['原因']}")
        if len(report.rejects) > 5:
            print(f"     …还有 {len(report.rejects) - 5} 行，见文件")
        print("   这份文件含真实姓名，按敏感数据处理。")

    return report


def main(argv: list[str] | None = None) -> int:
    console.setup()

    parser = argparse.ArgumentParser(description="清洗 data/raw/ 的数据并幂等入库")
    parser.add_argument("--kind", choices=["member", "participation"], required=True)
    parser.add_argument("--path", type=Path, default=None, help="指定文件，默认 data/raw/<kind>.csv")
    parser.add_argument("--dry-run", action="store_true", help="只报告结果，不写数据库")
    parser.add_argument("--create-activities", action="store_true",
                        help="活动不存在时自动新建（默认不建，记入丢弃清单）")
    args = parser.parse_args(argv)

    run(args.kind, path=args.path, dry_run=args.dry_run,
        create_activities=args.create_activities)
    return 0


if __name__ == "__main__":
    sys.exit(main())
