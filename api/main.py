"""FastAPI 入口。

先建表造数据：python -m scripts.init_db  然后  python -m scripts.seed
再启动：      python -m uvicorn api.main:app --reload --port 8000

网页：http://localhost:8000
接口文档：http://localhost:8000/docs   （FastAPI 自带的，可以直接在页面上点着试）
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import config, db, vectors as vec
from .routers import directory, experience, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭时干的事。

    ★ 模型在这里加载一次，之后常驻内存。
      embedding 模型加载要 15–30 秒，如果每次请求都加载，接口根本没法用。
      写在 lifespan 里，这 30 秒只付一次，付在启动时。
    """
    print("正在加载 embedding 模型（首次约 15–30 秒）…")
    model = vec.get_model()
    dim = vec.embedding_dim()
    print(f"模型就绪：{config.EMBEDDING_MODEL}（{dim} 维）")

    vec.get_collection()
    print(f"向量库就绪：{config.CHROMA_DIR}（已有 {vec.count()} 条经验）")

    yield

    print("服务已停止。")


app = FastAPI(
    title="学创部知识库",
    description="经验知识库（K1–K3）+ 活动分工数据库（K4–K5）",
    version="0.1.0",
    lifespan=lifespan,
)

# 演示阶段允许跨域，前端换端口调试时不用改配置。
# 上线前要收紧成具体域名，否则任何网站都能调这套接口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(experience.router)
app.include_router(directory.router)
app.include_router(reports.router)


@app.get("/api/health", response_model=None, tags=["自检"], summary="健康检查")
def health():
    """一次看三件事：数据库通不通、向量库有多少条、模型是什么。"""
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "已连接"
    except Exception as exc:  # noqa: BLE001 —— 自检接口，什么错都想知道
        database = f"连接失败：{exc.__class__.__name__}"

    try:
        model_name = config.EMBEDDING_MODEL
        dim = vec.embedding_dim()
        count = vec.count()
    except Exception as exc:  # noqa: BLE001
        model_name, dim, count = f"加载失败：{exc.__class__.__name__}", 0, 0

    return {
        "ok": database == "已连接",
        "database": f"{database}（{config.DB_NAME}）",
        "experience_count": count,
        "model": model_name,
        "embedding_dim": dim,
    }


# ── 演示页面 ──────────────────────────────────────────────────
WEB_DIR = config.PROJECT_ROOT / "web"

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
