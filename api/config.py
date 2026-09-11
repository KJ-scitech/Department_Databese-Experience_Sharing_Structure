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
