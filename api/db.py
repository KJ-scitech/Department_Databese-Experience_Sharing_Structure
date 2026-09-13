"""数据库连接：SQLAlchemy engine + session。

每个人连的是自己电脑上的 MySQL，密码从 .env 读，不共享。
"""
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

# 密码里可能含特殊字符（@ : / 等），必须转义后拼进连接串，
# 否则 SQLAlchemy 会把密码里的 @ 当成主机分隔符，报一个很难懂的错。
_url = (
    f"mysql+pymysql://{config.DB_USER}:{quote_plus(config.DB_PASSWORD)}"
    f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}?charset=utf8mb4"
)

engine = create_engine(
    _url,
    pool_pre_ping=True,  # 连接空闲久了会被 MySQL 断开，取用前先探一下
    pool_recycle=3600,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db():
    """FastAPI 依赖注入用：每个请求一个 session，用完自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
