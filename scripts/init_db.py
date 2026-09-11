"""建库建表：把 schema/schema.sql 执行一遍。

运行：python -m scripts.init_db

注意：非幂等。schema.sql 用的是裸 CREATE TABLE（不带 IF NOT EXISTS），
   库已存在时会报 1050 "table already exists"。
   想推倒重来用 python -m scripts.reset_db。

这个脚本取代了原来的 scripts/init-db.js（Node 版）。
schema 是关系模式的唯一来源，由部长维护，不要改。
"""
import re
import sys

import pymysql
from pymysql.constants import CLIENT

from api import config
from scripts import console

SCHEMA_FILE = config.PROJECT_ROOT / "schema" / "schema.sql"


def connect(with_database: bool = False):
    """连 MySQL。

    注意这里**不指定 database**——因为库可能还不存在，
    schema.sql 自己带了 CREATE DATABASE 和 USE。
    """
    kwargs = dict(
        host=config.DB_HOST,
        port=int(config.DB_PORT),
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
        # 必须开这个标志位：整份 schema.sql 是一条一条用分号隔开的，
        # 而 pymysql 不会替我们按分号切开、逐条发——它把整个字符串原样
        # 丢给服务端。不开 MULTI_STATEMENTS 的话，服务端读到第二个分号
        # 就报语法错误，一行表都建不出来。
        client_flag=CLIENT.MULTI_STATEMENTS,
    )
    if with_database:
        kwargs["database"] = config.DB_NAME
    return pymysql.connect(**kwargs)


def main() -> int:
    console.setup()

    if not SCHEMA_FILE.exists():
        print(f"失败：找不到 {SCHEMA_FILE}")
        return 1

    sql = SCHEMA_FILE.read_text(encoding="utf-8")

    # schema.sql 里写的库名和 .env 里的是不是同一个。
    # 不一致时不报错退出——以 schema 为准，但要说一声，
    # 否则「我明明配了 scitech_kb，怎么建出来一个别的库」能查半天。
    match = re.search(r"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?", sql, re.I)
    schema_db = match.group(1) if match else None
    if schema_db and schema_db != config.DB_NAME:
        print(f"注意：.env 里是 DB_NAME={config.DB_NAME}，但 schema.sql 建的是 {schema_db}")
        print(f"    以 schema.sql 为准，实际会建 {schema_db}。要改就改 .env。")
        print()

    print(f"正在执行 {SCHEMA_FILE.name} …")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            # 开了 MULTI_STATEMENTS 之后，服务端是逐个结果集返回的。
            # 不把剩下的结果集读完，后面那句 commit 会撞上
            # "Commands out of sync"。
            while cur.nextset():
                pass
        conn.commit()
    except pymysql.err.OperationalError as exc:
        code, msg = exc.args[0], exc.args[1] if len(exc.args) > 1 else exc
        print(f"失败：执行 schema.sql 出错（errno {code}）：{msg}")
        if code == 1050:
            print("   表已经存在了。要推倒重来请用：python -m scripts.reset_db")
        elif code == 1044:
            print("   权限不足：检查 .env 里的 DB_USER / DB_PASSWORD 对不对")
        elif code == 2003:
            print("   连不上 MySQL：确认服务在跑、端口号对不对")
        return 1
    except pymysql.err.ProgrammingError as exc:
        print(f"失败：SQL 报错：{exc.args[1] if len(exc.args) > 1 else exc}")
        return 1
    finally:
        conn.close()

    # 建完了，列一下确认
    conn = connect(with_database=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW FULL TABLES")
            rows = cur.fetchall()
    finally:
        conn.close()

    tables = sorted(r[0] for r in rows if r[1] == "BASE TABLE")
    views = sorted(r[0] for r in rows if r[1] == "VIEW")

    print(f"\n完成：建好了。库 {config.DB_NAME}")
    print(f"   表 {len(tables)} 张：{', '.join(tables)}")
    print(f"   视图 {len(views)} 个：{', '.join(views)}")
    print("\n   下一步：python -m scripts.seed   （造演示数据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
