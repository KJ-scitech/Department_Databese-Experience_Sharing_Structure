"""数据拉取：把在线表格里的数据取回本地。

运行（拉线上表格，需要先配好 .env）：
    python -m pipeline.fetch --kind member
    python -m pipeline.fetch --kind participation

运行（先拿一份本地文件练手，不需要账号）：
    python -m pipeline.fetch --kind member --from-file 某文件.csv

产出：data/raw/member.csv、data/raw/participation.csv
      格式由 pipeline/contract.py 约定，落盘编码 utf-8-sig。

── 开工第一件事 ─────────────────────────────────────────────

本文件里有两个「源列名 → 约定列名」的对照表（见下方 _SOURCE_COLUMNS）。
**这两个表里的源列名是占位的，必须对着真实表格核对一遍再改。**

核对方法：随便导出一行数据，看表头实际写的是什么。
是「姓名」还是「名字」，是「学号」还是「学生学号」——
差一个字就取不到值，而且不会报错，只会默默少一列。

── 关于线上接口 ─────────────────────────────────────────────

拉取走 SeaTable 的 REST 接口，路径与参数按公开文档写。
**本地无法验证**（要连内网的表格服务），第一次跑通前，
接口路径、分页字段名都可能需要按实际情况调整。
拿不准就找服务器管理员要一份「用 curl 能拉回一行」的命令，照着改最快。
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import requests

from api import config
from pipeline import contract
from scripts import console

# ── 源列名对照 ────────────────────────────────────────────────
# 左边是约定列名（contract 里定的），右边是**源表格里实际的表头文字**。
# 占位值需要核对。值为 None 表示「这张表里没有这一列」，会被跳过。
_SOURCE_COLUMNS = {
    "member": {
        "name": "姓名",
        "student_no": "学号",
        "gender": "性别",
        "grade": "年级",
        "major": "专业",
        "join_term": "入部学年",
        "org_unit": "所属组",
        "title": "职务",
        "phone": "手机",
        "qq": "QQ",
        "wechat": "微信",
        "email": "邮箱",
        "skills": "技能",
        "bio_short": "一句话简介",
    },
    "participation": {
        "activity_name": "项目名称",
        "activity_series": "所属系列",
        "term": "学年学期",
        "event_date": "活动日期",
        "location": "地点",
        "activity_status": "状态",
        "member_name": "对接人",
        "role": "分工",
        "contribution": "工作内容",
        "hours": "工时",
        "honor": "评优",
    },
}

# 源表里不同的表名，分别对应哪种记录
_TABLES = {
    "member": lambda: config.SEATABLE_MEMBER_TABLE,
    "participation": lambda: config.SEATABLE_LEDGER_TABLE,
}

_FIELDS = {
    "member": contract.MEMBER_FIELDS,
    "participation": contract.PARTICIPATION_FIELDS,
}

_PAGE_SIZE = 100


class ConfigMissing(Exception):
    """线上表格的地址或 token 没配。"""


def _require_online_config(kind: str) -> str:
    """检查 .env 配全了没有，返回要拉的表名。

    缺什么就直说缺什么——比抛一个 KeyError 让人去猜强。
    """
    missing = []
    if not config.SEATABLE_BASE_URL:
        missing.append("SEATABLE_BASE_URL  线上表格的地址")
    if not config.SEATABLE_TOKEN:
        missing.append("SEATABLE_TOKEN     访问 token（找服务器管理员要）")
    if not config.SEATABLE_DTABLE_UUID:
        missing.append("SEATABLE_DTABLE_UUID  表格的 uuid")

    table = _TABLES[kind]()
    if not table:
        key = "SEATABLE_MEMBER_TABLE" if kind == "member" else "SEATABLE_LEDGER_TABLE"
        missing.append(f"{key}   要拉的表的表名")

    if missing:
        raise ConfigMissing(
            "线上的地址/token 还没配好，.env 里缺：\n"
            + "\n".join(f"    {m}" for m in missing)
            + "\n\n  这些是敏感信息，只放 .env（不进 Git），不要写进代码。"
            + "\n  没有账号也能先干活：加 --from-file 用本地文件跑。"
        )
    return table


def fetch_rows(table: str, *, base_url: str, token: str, uuid: str,
               page_size: int = _PAGE_SIZE) -> list[dict]:
    """分页把一张表拉全。

    分页是必须的：接口一次最多返回 100 行左右，
    不分页的话数据一多就只会拿到第一页，而且**不会报错**。
    这个坑比报错阴险，所以这里默认按页循环到底。
    """
    url = f"{base_url.rstrip('/')}/api/v2/dtables/{uuid}/rows/"
    headers = {"Authorization": f"Token {token}"}
    rows: list[dict] = []
    start = 0

    while True:
        params = {"table_name": table, "start": start, "limit": page_size}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise ConfigMissing("token 被拒了（401）。找服务器管理员确认 token 是否还有效。")
        resp.raise_for_status()

        payload = resp.json()
        page = payload.get("rows", [])
        rows.extend(page)

        if len(page) < page_size:
            # 最后一页。不靠"返回空列表"来判断——某些接口会在最后一页
            # 仍然返回一批数据，用「这一页没满」判断更稳。
            break
        start += page_size

    return rows


def map_columns(raw_rows: list[dict], kind: str) -> list[dict]:
    """把源列名换成约定列名。"""
    mapping = _SOURCE_COLUMNS[kind]
    out = []
    for raw in raw_rows:
        row = {}
        for target, source in mapping.items():
            if source is None:
                continue
            row[target] = raw.get(source)
        out.append(row)
    return out


def write_csv(rows: list[dict], kind: str, path: Path) -> None:
    """按约定列顺序写 CSV。"""
    fields = _FIELDS[kind]
    names = [f.name for f in fields]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=contract.ENCODING, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_csv(path: Path) -> list[dict]:
    """读一份本地 CSV，返回 [{列名: 值}]。

    utf-8-sig 会自动吃掉 Excel 写的 BOM；
    如果文件其实是 GBK，再退回 GBK 试一次——国内的表格软件
    默认导出 GBK 很常见，不兜这一下会读到乱码而不是报错。
    """
    for encoding in (contract.ENCODING, "gbk"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                return list(csv.DictReader(fh))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "utf-8/gbk", b"", 0, 1,
        f"读不出 {path}，两种编码都试过了。另存为 CSV UTF-8 再试。",
    )


def run(kind: str, from_file: Path | None = None) -> int:
    fields = _FIELDS[kind]

    if from_file is not None:
        if not from_file.exists():
            print(f"失败：找不到文件 {from_file}")
            return 1
        rows = read_csv(from_file)
        print(f"从本地文件读入 {len(rows)} 行：{from_file}")
        # 本地文件按约定，列名已经是英文的，不用再映射
    else:
        table = _require_online_config(kind)
        print(f"从线上表格拉取：{table}")
        raw = fetch_rows(
            table,
            base_url=config.SEATABLE_BASE_URL,
            token=config.SEATABLE_TOKEN,
            uuid=config.SEATABLE_DTABLE_UUID,
        )
        rows = map_columns(raw, kind)
        print(f"取回 {len(rows)} 行")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = config.RAW_DIR / f"{kind}.csv"
    write_csv(rows, kind, out)
    # 再存一份带时间戳的：线上数据随时会变，
    # 出了问题要能回答「上次拉的是哪一版」。这份不进 Git。
    write_csv(rows, kind, config.RAW_DIR / f"{kind}-{stamp}.csv")

    print(f"完成：写入 {out}")
    print(f"      {len(rows)} 行，{len(fields)} 列")
    print(f"\n   下一步：python -m pipeline.clean --kind {kind}")
    return 0


def main(argv: list[str] | None = None) -> int:
    console.setup()

    parser = argparse.ArgumentParser(description="从在线表格拉取数据到 data/raw/")
    parser.add_argument("--kind", choices=["member", "participation"], required=True,
                        help="拉哪种记录：member 成员 / participation 参与记录")
    parser.add_argument("--from-file", type=Path, default=None,
                        help="改用本地文件，不连线上表格（没账号时用）")
    args = parser.parse_args(argv)

    try:
        return run(args.kind, args.from_file)
    except ConfigMissing as exc:
        print(f"失败：{exc}")
        return 1
    except requests.RequestException as exc:
        print(f"失败：请求出错 {exc}")
        print("   检查：网络能不能通到表格服务、地址对不对、token 过没过期。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
