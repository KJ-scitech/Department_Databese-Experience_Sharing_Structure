"""配置：从项目根目录的 .env 读取数据库等信息。

.env 里存的是各人自己电脑上的数据库密码，不进 Git。
模板见项目根的 .env.example。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = 本文件的上上级（api/config.py → api/ → 知识库/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# ── 数据库 ────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "scitech_kb")

# ── 服务端口 ──────────────────────────────────────────────────
# 注意：.env 里的 PORT 是给旧的 Node 骨架用的（默认 3000）。
# 新的 Python 服务用 8000，避免和它撞车。
PORT = int(os.getenv("API_PORT", "8000"))

# ── 数据源（数据管道线用）────────────────────────────────────
# 在线表格的地址和 token 只放 .env，绝不写进代码、文档、聊天记录。
# 没配也能干活——pipeline/fetch.py 支持 --from-file 用本地文件跑。
SEATABLE_BASE_URL = os.getenv("SEATABLE_BASE_URL", "")
SEATABLE_TOKEN = os.getenv("SEATABLE_TOKEN", "")
SEATABLE_DTABLE_UUID = os.getenv("SEATABLE_DTABLE_UUID", "")
SEATABLE_MEMBER_TABLE = os.getenv("SEATABLE_MEMBER_TABLE", "")
SEATABLE_LEDGER_TABLE = os.getenv("SEATABLE_LEDGER_TABLE", "")

# ── 网页写接口的口令 ──────────────────────────────────────────
# 页面要给部员用，就得绑到校园网上；绑出去之后，写接口（录经验、登记分工、
# 新增成员）再没有门槛，等于谁知道地址谁都能改。
#
# 这里放一个**部门共享口令**：部员在页面上输一次，之后靠 cookie 记住。
# 它只挡「校园网里乱逛进来的人」——不区分是谁写的，也没有按人限权。
# 要真身份就得上学校的 CAS，schema 里的 account 表就是为那个准备的。
#
# 不配这个值 = 鉴权整个关闭（本地开发、跑测试时就是这样），
# 启动时会大声警告一句。服务器上的 .env 必须配上。
def shared_password() -> str:
    """每次调用现读，不缓存在模块常量里。

    ★ 为什么不用 `KB_SHARED_PASSWORD = os.getenv(...)` 那种写法：
      测试要用 monkeypatch 换成别的值来验「配上口令之后写接口会被拦」，
      而模块常量在 import 那一刻就固化了，之后再改环境变量没有任何作用。
      这个问题和 api/config.py 里 HF_ENDPOINT 那条注释说的是同一件事。
    """
    return os.getenv("KB_SHARED_PASSWORD", "")


def cors_origins() -> list[str]:
    """允许跨域的来源，逗号分隔，从 .env 读。

    ★ 为什么默认是空的、而不是像以前那样写 `["*"]`：
      这个仓库是公开的，服务器 IP 属于基础设施信息，不能写进代码
      （deploy/scitech-kb.service 里用 <共享账号> 占位也是同一个理由）。
      服务器上的 .env 写 KB_CORS_ORIGINS=http://<服务器内网IP>:8000 这样。

    ★ 留空时**不加 CORS 中间件**：页面和接口是同一个源，本来就不需要跨域。
      只有本地换端口调试时才用得上。
    """
    raw = os.getenv("KB_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


# ── 向量库 ────────────────────────────────────────────────────
DATA_DIR = PROJECT_ROOT / "data"
CHROMA_DIR = DATA_DIR / "chroma"

# 数据管道的工作目录。都落在 data/ 下，已被 .gitignore 拦住——
# 里面的内容是真实成员的姓名学号，属于绝对不能进仓库的那一类。
RAW_DIR = DATA_DIR / "raw"          # 拉回来的原始数据
REJECTS_DIR = DATA_DIR / "rejects"  # 被丢弃的行及原因（不许静默丢弃）

# 坑 3：Chroma 的集合名只允许 [a-zA-Z0-9._-]，不能用中文。
# 所以集合叫 experience，中文只放文档内容里。
COLLECTION_NAME = "experience"

# 中文小模型，512 维，约 93 MB。
# 首次运行会自动下载，之后走本地缓存（~/.cache/huggingface）。
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

# HuggingFace 国内镜像。直连 huggingface.co 在国内基本不通，
# 必须在导入 sentence-transformers 之前设置好这个环境变量。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 关掉 HuggingFace 的 xet 传输协议。
# 国内下这个模型走 xet 会**卡住、或者直接失败**，报的错又跟网络没关系，极难查。
# 关掉之后退回普通 HTTP 下载，就正常了。
# 和上面那条一样，必须在 huggingface_hub 被导入**之前**设置——
# 它在导入的那一刻就把这个值读成模块常量了，之后再设没有任何作用。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
