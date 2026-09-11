"""测试公共夹具。

三件事：
  1. 让 `import api` / `import pipeline` 能找到——pytest 默认不一定把
     项目根目录加进 sys.path，这里显式加一次，免得换个跑法就 import 失败。
  2. 提供 client 夹具（FastAPI 的测试客户端）。
  3. 提供几个「前置条件没满足就跳过」的守卫，跳过时给一句人话，
     而不是抛一堆看不懂的栈。

★ client 是 session 级的：整个测试过程只启动一次应用。
  因为启动时要加载 embedding 模型，约 15–30 秒。
  如果改成每个测试函数起一次，跑一轮测试要等十几分钟，没人会愿意跑。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def client():
    """FastAPI 测试客户端。

    用 `with` 而不是直接构造：只有进到 with 里才会跑 lifespan，
    也就是才会加载模型、打开向量库。不用 with 的话，
    请求打进去会发现向量库根本没初始化。
    """
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def seeded(client):
    """确认库里已有演示数据；没有就跳过，并说明怎么造。

    返回的就是 client，所以测试里可以直接写 `seeded.get(...)`。
    """
    from api import vectors as vec

    if vec.count() == 0:
        pytest.skip(
            "向量库是空的，这些用例没有数据可验。\n"
            "   先造数据：python -m scripts.seed"
        )
    return client


@pytest.fixture
def session():
    """直接操作数据库用（数据管道线的测试要）。"""
    from api import db

    with db.SessionLocal() as s:
        yield s


@pytest.fixture(scope="session")
def tables_ready():
    """确认表建好了。没建就跳过并说明命令。

    ★ 这个夹具**故意不依赖 client**。
      依赖 client 就等于要跑 lifespan、要加载 embedding 模型（20–30 秒）。
      数据管道线的测试只碰数据库，用不着模型，没必要跟着等。
    """
    from sqlalchemy import text

    from api import db

    with db.engine.connect() as conn:
        rows = conn.execute(text("SHOW FULL TABLES")).all()

    names = {row[0] for row in rows}
    if "member" not in names:
        pytest.skip(
            "数据库里还没有表。\n"
            "   先建表：python -m scripts.init_db"
        )
    return True
