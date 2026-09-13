"""删库重建：丢掉整个库，再重新执行 schema.sql。

运行：python -m scripts.reset_db        （会先问一句确认）
      python -m scripts.reset_db -y     （跳过确认，脚本化时用）

注意：这是**破坏性操作**，库里所有数据都会没。只在本地开发库上用。
   服务器上的库别碰——真要重建，走 DBA 流程。

这个脚本取代了原来的 scripts/reset-db.js（Node 版）。
"""
import sys

import pymysql

from api import config
from scripts import console, init_db

# 只查这几张表够不够？够。它们是 schema 里唯三装业务数据的表，
# 剩下 4 张（member_position / privacy_consent / account）平时是空的。
COUNTED_TABLES = ("member", "activity_series", "activity", "participation")


def peek() -> dict:
    """看一眼库里现在有什么，返回 {表名: 行数}。

    库不存在、或者表还没建，都返回空 dict——这不是错误，
    反而是最常见的情况（第一次跑）。
    """
    try:
        conn = init_db.connect(with_database=True)
    except pymysql.err.OperationalError as exc:
        if exc.args and exc.args[0] == 1049:  # Unknown database
            return {}
        raise

    counts = {}
    try:
        with conn.cursor() as cur:
            for table in COUNTED_TABLES:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    counts[table] = cur.fetchone()[0]
                except pymysql.err.ProgrammingError:
                    # 表不存在（比如上次建到一半失败了），跳过
                    counts[table] = None
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    console.setup()

    argv = sys.argv[1:] if argv is None else argv
    assume_yes = "-y" in argv or "--yes" in argv

    counts = peek()
    if not counts:
        print(f"库 {config.DB_NAME} 不存在或还没有表，直接建。")
    else:
        print(f"注意：即将删除库 {config.DB_NAME}，里面的数据：")
        for table, n in counts.items():
            shown = "表不存在" if n is None else f"{n} 行"
            print(f"     {table:<18} {shown}")
        print()

        if not assume_yes:
            answer = input("确认删除并重建？输入 yes 继续：").strip().lower()
            if answer != "yes":
                print("已取消，什么都没动。")
                return 1

    conn = init_db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{config.DB_NAME}`")
        conn.commit()
    finally:
        conn.close()
    print(f"已删除 {config.DB_NAME}。\n")

    # 复用 init_db，不再抄一遍建表逻辑。
    # init_db 自己会打印建了哪 7 表 2 视图。
    return init_db.main()


if __name__ == "__main__":
    sys.exit(main())
