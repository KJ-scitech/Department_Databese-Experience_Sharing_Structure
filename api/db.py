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

# ★ 上面没配任何 ssl 参数，但这条连接实际是走 TLS 的——pymysql 的默认行为
#   （MySQL 8 初始化时会自动生成自签证书，服务端这一侧天然支持）。
#   实测：SHOW STATUS LIKE 'Ssl_cipher' → TLS_AES_256_GCM_SHA384。
#
#   这解释了一件容易踩的事：requirements.txt 里**没有 cryptography**，
#   却一直连得上。因为 TLS 之下 caching_sha2_password 直接把密码送过去、
#   不走 RSA，用不到它（pymysql/_auth.py 那句 "'cryptography' package is
#   required for caching_sha2_password" 只在**非 TLS 的 full auth** 上才触发）。
#   真要把 TLS 断掉（ssl_disabled=True）再去连一个服务端密码缓存里没有的账号，
#   就会抛那句 RuntimeError——报的错跟「依赖没装」看着毫无关系，很难查。
#   要走那条路，先装 cryptography 再断 TLS。
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
