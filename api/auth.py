"""共享口令鉴权：把从校园网来的写请求拦在门外。

页面要给部员用，服务就得绑到校园网（`--host 0.0.0.0`）。绑出去之后，
后端的写接口（录经验、登记分工、新增成员）原本是一点门槛都没有的——
谁知道地址谁就能改。这个模块补上那道门。

口令是**部门共享**的：部员在页面上输一次，之后靠 cookie 记住。
它只挡「校园网里乱逛进来的人」，既不区分是谁写的，也不能按人限权。
要真身份得接学校的 CAS（`account` 表就是为那个建的），那是下一步。

── 为什么用中间件，而不是给每个写接口加依赖 ──────────────────

★ **fail-closed**：以后新加的写接口自动被拦住，不存在"忘了加装饰器"。
  一个接口忘了加鉴权，表现和"鉴权生效"一模一样——不报错、不告警，
  只有真正被人写脏数据的时候才知道。靠人记得住是靠不住的。

★ `api/routers/directory.py` 被 PR #5 占着，`ingest/` 被 PR #4 占着。
  中间件加在 `main.py`，两个文件都不用碰，将来合并不会打架。

── 规则（只对 /api/ 下的 POST / PUT / PATCH / DELETE 生效，GET 一律放行）──

  1. .env 里没配 KB_SHARED_PASSWORD  → 放行（本地开发），启动时大声警告
  2. 客户端在回环地址（127.0.0.1 / ::1）→ 放行
  3. 带了正确的 X-KB-Password 请求头  → 放行（中文口令要百分号编码，见下）
  4. 带了正确的登录 cookie            → 放行
  5. 其余                             → 401

★ 第 2 条（回环免口令）是个**有意的取舍**，不是漏掉：

  机器人（AstrBot）和 CLI 都在同一台机器上，走 127.0.0.1 调这套接口。
  不给它们免口令，就得改 `cli/client.py` 的传输层签名——
  而 `tests/test_cli.py` 里的 FakeTransport 是五个位置参数、没有 **kwargs，
  改签名会把那个文件里的用例全打挂，而那个文件不归我们改。

  代价说清楚：**服务器上**任何一个本地账号都能往 127.0.0.1:8000 写，
  不需要知道口令。这在共享的 M710q 上不算零风险。
  真正要收口的时候，给 CLI 加上请求头、把这里第 2 条删掉就行。

★ 走 HTTP，没上 TLS，所以口令和 cookie 在校内网里是**明文**的。
  校园网内被嗅探就能拿到。缓解办法是上 HTTPS（自签证书要部员装根证书，
  摩擦不小），暂时没做——这件事得让用的人知道，不能假装不存在。
"""
import hashlib
import hmac
import time
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config

# ── 会话 cookie ───────────────────────────────────────────────
COOKIE_NAME = "kb_session"

# 7 天。共享口令很少换，到期时间短了部员天天重输；
# 长了的话，cookie 泄露之后的暴露窗口也长。两边取个中。
SESSION_TTL_SECONDS = 7 * 24 * 3600

# 免鉴权的路径：这两个是"拿口令"和"退出口令"本身，拦了就进不去。
EXEMPT_PATHS = {"/api/auth/login", "/api/auth/logout"}

# 需要口令的方法。GET/HEAD/OPTIONS 全放行——读是给部员用的，那是这套东西的意义。
GUARDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 回环地址。`::ffff:127.0.0.1` 是 IPv6 映射写法，双栈监听时会出现。
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


# ── 令牌签发与校验 ────────────────────────────────────────────

def _secret(password: str) -> bytes:
    """签名密钥由口令派生。

    ★ 这样就不用再多配一个"密钥"项，而且**换了口令，所有已发出的 cookie
      立刻失效**——共享口令的换法本来就该是"所有人重新登一次"。
      白捡一个会话失效机制。
    """
    return hashlib.sha256(("kb-session-v1:" + password).encode("utf-8")).digest()


def make_token(password: str, now: float | None = None) -> str:
    """签一个 `到期时间.签名` 形式的令牌。"""
    expires = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    sig = hmac.new(_secret(password), f"v1:{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def check_token(password: str, token: str | None, now: float | None = None) -> bool:
    """验令牌。过期、格式不对、签名不对，一律 False。"""
    if not token:
        return False
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except (ValueError, AttributeError):
        return False

    now = now if now is not None else time.time()
    if expires <= now:
        return False

    want = hmac.new(_secret(password), f"v1:{expires}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def _same(a: str, b: str) -> bool:
    """定长时间比较，避免靠响应时间一个字节一个字节地猜口令。

    ★ 必须先 encode 成 bytes 再比。`compare_digest` 对**非 ASCII 的 str
      直接抛 TypeError**（"comparing strings with non-ASCII characters is
      not supported"）——口令里只要有一个汉字，这个函数就炸。
      而这里多半正是一个中文口令。这个坑不写下来一定会踩。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── 判断一个请求要不要拦 ──────────────────────────────────────

def _is_loopback(request: Request) -> bool:
    client = request.client
    return bool(client) and client.host in LOOPBACK


def _credentials_ok(request: Request, password: str) -> bool:
    header = request.headers.get("X-KB-Password")
    if header:
        # ★ 请求头只能是 ASCII——**HTTP 协议就这么定的**，中文口令塞进去，
        #   客户端（requests / httpx / 浏览器）在发出去之前就会抛
        #   UnicodeEncodeError: 'ascii' codec can't encode...
        #   所以这里同时接受百分号编码：中文口令写成
        #   `%E9%83%A8%E9%97%A8...` 也能对上（用 quote() 编）。
        #
        #   两种都比一遍，而不是"先 unquote 再比"：
        #   口令里要是正好含 `%41` 这样的片段，unquote 会把它解成 `A`，
        #   那样反而比对不上了。两边都试没有这个副作用。
        #
        #   页面登录（cookie）那条路不受影响：cookie 里放的是签名后的令牌，
        #   纯 ASCII，跟口令本身是不是中文无关。
        if _same(header, password) or _same(unquote(header), password):
            return True
    return check_token(password, request.cookies.get(COOKIE_NAME))


async def guard(request: Request, call_next):
    """中间件本体。放行或 401，不做别的。"""
    path = request.url.path
    if (
        request.method not in GUARDED_METHODS
        or not path.startswith("/api/")
        or path in EXEMPT_PATHS
    ):
        return await call_next(request)

    password = config.shared_password()
    if not password:            # 没配 = 本地开发，鉴权关闭
        return await call_next(request)
    if _is_loopback(request):   # 机器人 / CLI，本机
        return await call_next(request)
    if _credentials_ok(request, password):
        return await call_next(request)

    return JSONResponse(
        {"detail": "这个操作要口令。请先在上面输入部门口令。"},
        status_code=401,
    )


# ── 登录 / 退出 / 自检 ────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["鉴权"])


class LoginIn(BaseModel):
    """登录请求体。

    ★ 这个模型**故意留在这个文件里**，没放进 `api/schemas.py`——
      鉴权相关的定义集中在一个文件里，审计的时候只用看这一份。
      业务模型该放哪还放哪。
    """

    password: str = Field(min_length=1, max_length=200)


def _login_state(request: Request, password: str) -> dict:
    """告诉前端现在是什么状态，好决定要不要显示写入口。"""
    enabled = bool(password)
    return {
        "auth_enabled": enabled,
        "logged_in": (not enabled)
        or _is_loopback(request)
        or check_token(password, request.cookies.get(COOKIE_NAME)),
    }


@router.get("/me", summary="当前是否已登录")
def me(request: Request):
    """页面加载时先问一句，决定写入口显示还是隐藏。

    ★ 隐藏入口**不是**安全措施，服务端该拦还是拦（guard 里那一套）。
      它只是别让部员点了半天才收到一个 401。
    """
    return _login_state(request, config.shared_password())


@router.post("/login", summary="用部门口令登录")
def login(payload: LoginIn, request: Request, response: Response):
    password = config.shared_password()

    if not password:
        # 没配口令：本地开发。老实说清，别让人以为"登录成功了"。
        return {"auth_enabled": False, "logged_in": True, "detail": "本地开发模式，未启用口令。"}

    if not _same(payload.password, password):
        raise HTTPException(status_code=401, detail="口令不对。")

    response.set_cookie(
        COOKIE_NAME,
        make_token(password),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,      # JS 读不到，XSS 偷不走
        samesite="strict",  # ★ 关键：跨站请求不带这个 cookie，等于顺手把 CSRF 堵了
        path="/",
        # secure=True 只在 HTTPS 下能用。现在走 HTTP，开了这个 cookie 根本发不出去。
        # 哪天上了 HTTPS，这里要改成 True。
        secure=False,
    )
    return {"auth_enabled": True, "logged_in": True}


@router.post("/logout", summary="退出")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"logged_in": False}
