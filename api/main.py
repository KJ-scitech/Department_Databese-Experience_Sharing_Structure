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
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth, config, db, schemas, vectors as vec
from .routers import cards, directory, experience, reports


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

    # ★ 启动时把鉴权状态说清楚。
    #   这里必须"大声"：没配口令时一切照常工作、接口照样返回 200，
    #   只有等绑到校园网、被人写进脏数据了才会发现——
    #   而那时候已经晚了。宁可每次启动吵一句。
    if config.shared_password():
        print("鉴权：已启用（写接口要口令；本机 127.0.0.1 免口令，机器人走这条）")
    else:
        print("!" * 60)
        print("鉴权：**未启用** —— .env 里没有 KB_SHARED_PASSWORD。")
        print("      写接口现在不设防。绑 0.0.0.0 之前必须先配上这个值，")
        print("      否则校园网里谁知道地址谁都能改数据。")
        print("!" * 60)

    yield

    print("服务已停止。")


app = FastAPI(
    title="学创部知识库",
    description="经验知识库（K1–K3）+ 活动分工数据库（K4–K5）",
    version="0.1.0",
    lifespan=lifespan,
)

# ── 中间件 ────────────────────────────────────────────────────
# ★ 顺序在这里有讲究，而且反了不会报错、只会症状古怪：
#   Starlette 的 add_middleware 是**往前插**的，最后加的在最外层。
#   所以先加 guard（内层）、后加 CORS（外层）——这样 guard 直接返回的
#   401 也会经过 CORS 那一层，浏览器才看得见状态码；
#   反过来加的话，401 没有跨域头，前端只会看到一句语焉不详的网络错误。
#
# guard 拦的是 /api/ 下所有写方法（POST/PUT/PATCH/DELETE），GET 放行。
# 为什么用中间件而不是给每个接口加依赖，见 api/auth.py 开头的说明。
app.add_middleware(BaseHTTPMiddleware, dispatch=auth.guard)

# 跨域。**默认不加**——页面和接口同源，本来就不需要。
# 只有本地换端口调试、或者服务器上要从别的源访问时才配 KB_CORS_ORIGINS。
# 原来是 allow_origins=["*"]（演示阶段留下的），现在绑校园网了，那是敞开的。
_origins = config.cors_origins()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,   # cookie 要跟着走，所以来源必须写死、不能用 "*"
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(experience.router)
app.include_router(directory.router)
app.include_router(reports.router)
app.include_router(cards.router)  # K6 介绍卡


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


@app.get("/api/meta", tags=["自检"], summary="枚举值（前端下拉框用）")
def meta():
    """把枚举值集中吐出来，前端下拉框不再各写一份。

    ★ 这个接口**必须**放在 main.py，不能放 experience router。
      经验路由里先注册了 GET /{exp_id}（匹配任意字符串），FastAPI 按注册顺序匹配，
      之后再注册 /meta 会被那个通配路径吞掉——症状是「查类型列表返回 404，
      详情说 exp-999 不存在」，看着像缓存问题，其实是路由顺序。

    ★ 这些值只用来填下拉框，服务端**不拿它们做校验**（EXPERIENCE_TYPES 除外，
      那条在 POST 里真的会拦）。理由：写接口现在没有鉴权，
      机器人那边可能已经在用「主持」「签到」这类写法，硬拦会把合法记录挡在门外。
    """
    return {
        "experience_types": list(schemas.EXPERIENCE_TYPES),
        "participation_roles": list(schemas.PARTICIPATION_ROLES),
        "member_status": list(schemas.MEMBER_STATUS),
        "activity_status": list(schemas.ACTIVITY_STATUS),
        # 职务的展示顺序（主席在前、部员在后）。前端拿它给成员列表和下拉框排序，
        # 这样"职务高的排前面"只有一份定义，接口和页面不会各排各的。
        "title_order": list(schemas.TITLE_ORDER),
    }


# ── 演示页面 ──────────────────────────────────────────────────
WEB_DIR = config.PROJECT_ROOT / "web"

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
