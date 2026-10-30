"""共享口令鉴权（api/auth.py）的测试。

★ 这个文件**故意不加载模型、不连数据库**。

  绝大多数用例只想知道「这个请求是被拦了还是放进去了」，而中间件跑在
  路由匹配和参数校验**之前**——所以拿一个必然校验失败的请求体去打写接口就够了：

      没被拦 → 422（请求体不合法的常规报错）
      被拦了 → 401

  这个 401 和 422 的差别，就是那扇门开没开。不用造数据、不用等模型，
  整组用例秒级跑完。

★ 为什么要单独测这个：鉴权是**唯一一类"坏了也不报错"的功能**。
  门开了的时候一切正常，接口照常返回 200——只有等校园网里有人把数据
  写脏了才知道。所以这里的用例要覆盖到每一种"以为关上了其实开着"的写法。
"""
import time
from urllib.parse import quote

import pytest

from api import auth

WRITE_PATH = "/api/experience"   # POST，落在 guard 的拦截范围内
BAD_BODY = {}                    # 必然校验失败：放行时得到 422，被拦时得到 401

PASSWORD = "部门口令测试用"


def _client(host: str = "203.0.113.9"):
    """造一个测试客户端，来源地址可控。

    ★ 不用 conftest 里的 client 夹具：那个会跑 lifespan、加载 embedding 模型，
      而这组用例只是想验门的开合，为它等 20 秒不值当。

    默认来源给一个**公网地址**，不是 `127.0.0.1`——
      TestClient 的默认来源是 `testclient`，本来就既不是回环也不像真实地址，
      不如显式写成一个明确的"校外"地址，读起来意图清楚。
    """
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app, client=(host, 51234))


@pytest.fixture
def remote():
    """一个"从校园网来的"客户端。"""
    return _client()


# ══ 口令没配：鉴权关闭 ═══════════════════════════════════════

def test_没配口令时写接口放行(remote, monkeypatch):
    """本地开发和跑测试时就是这样，接口不设防。

    这条是**回归保护**：反过来说，如果哪天有人把默认值改成"没配就拦"，
      所有本地开发和 CI 会立刻全部 401，而报的错会指向接口本身。
    """
    monkeypatch.delenv("KB_SHARED_PASSWORD", raising=False)

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 422


# ══ 配了口令：远端写请求必须有凭证 ═══════════════════════════

def test_配了口令后远端写请求被拦(remote, monkeypatch):
    """这条是整个功能的核心。"""
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    resp = remote.post(WRITE_PATH, json=BAD_BODY)

    assert resp.status_code == 401
    assert "口令" in resp.json()["detail"]


def test_所有写方法都被拦(remote, monkeypatch):
    """★ 只拦 POST 是个经典的漏法。

    标签是用 PATCH 改的、经验是用 DELETE 删的——只守 POST 等于
      "能改能删但不能加"，比完全不设防还难察觉。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401
    assert remote.patch(f"{WRITE_PATH}/exp-1", json={"tags": []}).status_code == 401
    assert remote.delete(f"{WRITE_PATH}/exp-1").status_code == 401
    assert remote.post("/api/members", json={}).status_code == 401


def test_中文口令走请求头要百分号编码(remote, monkeypatch):
    """★ 这条是写这组用例时才发现的**真问题**，不是测试写法问题。

    HTTP 请求头只能是 ASCII。中文口令直接塞进 `X-KB-Password`，
      客户端（requests / httpx / 浏览器）**在发出去之前**就抛
      `UnicodeEncodeError: 'ascii' codec can't encode...`——
      请求根本到不了服务端，排查的时候会以为是自己代码写错了。

    所以服务端同时接受百分号编码。给命令行用的时候：

        headers={"X-KB-Password": quote(口令)}

    cookie 那条路（页面登录）不受影响：cookie 里放的是签名后的令牌，
      纯 ASCII，跟口令本身是不是中文无关。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    resp = remote.post(WRITE_PATH, json=BAD_BODY,
                       headers={"X-KB-Password": quote(PASSWORD)})

    assert resp.status_code == 422, "百分号编码的中文口令没被认出来"


def test_错的请求头被拦(remote, monkeypatch):
    """★ 这里故意用一个**纯 ASCII 的错口令**。

    用中文错口令的话，httpx 会在客户端就抛 UnicodeEncodeError，
      测的就不是"服务端拦没拦"了。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    resp = remote.post(WRITE_PATH, json=BAD_BODY, headers={"X-KB-Password": "wrong-password"})

    assert resp.status_code == 401


# ══ 登录 / cookie ════════════════════════════════════════════

def test_登录后拿到cookie就能写(remote, monkeypatch):
    """部员在页面上走的完整路径：输口令 → 拿到 cookie → 之后就能写。"""
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    assert remote.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 422


def test_口令错了登录被拒(remote, monkeypatch):
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    resp = remote.post("/api/auth/login", json={"password": "猜的"})

    assert resp.status_code == 401
    # 没登进来就不该发 cookie
    assert "kb_session" not in resp.cookies


def test_没登录时cookie不会被凭空认可(remote, monkeypatch):
    """塞一个自己编的 cookie 不该管用。

    ★ cookie 的值用纯 ASCII 编：cookie 和请求头一样不能放中文，
      用中文伪造串的话 httpx 在客户端就抛错了，测不到服务端。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)
    remote.cookies.set(auth.COOKIE_NAME, "9999999999.deadbeefdeadbeefdeadbeefdeadbeef")

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401


def test_退出之后写不了(remote, monkeypatch):
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    remote.post("/api/auth/login", json={"password": PASSWORD})
    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 422

    remote.post("/api/auth/logout")

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401


# ══ 令牌本身：过期、篡改、换口令 ═════════════════════════════

def test_过期令牌不认(remote, monkeypatch):
    """★ 直接拿 auth 的签证函数造一个已过期的令牌。

    这条只有把过期判断真的写出来才能过——把有效期设成 100 年、
      或者干脆不检查过期时间，功能测试是发现不了的。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)
    stale = auth.make_token(PASSWORD, now=time.time() - auth.SESSION_TTL_SECONDS - 60)

    remote.cookies.set(auth.COOKIE_NAME, stale)

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401


def test_到期时间被改大也不认(remote, monkeypatch):
    """把 `到期.签名` 里的到期时间往后改，签名就对不上了。"""
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)
    good = auth.make_token(PASSWORD)
    forged = f"{int(time.time()) + 10**9}.{good.split('.', 1)[1]}"

    remote.cookies.set(auth.COOKIE_NAME, forged)

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401


def test_换了口令旧cookie立刻失效(remote, monkeypatch):
    """密钥由口令派生，所以换口令 = 所有人重新登一次。

    这是白捡的会话失效机制。哪天有人把密钥改成写死的常量，这条就红了。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)
    remote.post("/api/auth/login", json={"password": PASSWORD})

    monkeypatch.setenv("KB_SHARED_PASSWORD", "换成了新口令")

    assert remote.post(WRITE_PATH, json=BAD_BODY).status_code == 401


def test_中文口令能登录(remote, monkeypatch):
    """★ 钉住 `hmac.compare_digest` 对非 ASCII 字符串会抛 TypeError 这个坑。

    写这个模块的时候第一版就是用 `compare_digest(str, str)` 直接比的——
      English 口令测试全过，换成中文口令，服务端直接 500。
      而这里多半正是一个中文口令。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", "部里的口令，有标点也有汉字")

    resp = remote.post("/api/auth/login", json={"password": "部里的口令，有标点也有汉字"})

    assert resp.status_code == 200, f"中文口令登录失败：{resp.text}"


# ══ 免口令的两类：回环地址、读接口 ═══════════════════════════

def test_回环地址免口令(monkeypatch):
    """★ 机器人（AstrBot）和 CLI 都在同一台机器上，走 127.0.0.1。

    给它们不免口令就得改 cli/client.py 的传输层签名，而
      tests/test_cli.py 里的 FakeTransport 是五个位置参数、没有 **kwargs，
      改签名会把那个文件打挂。这条测试把这个取舍固定下来。
    """
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    for host in ("127.0.0.1", "::1"):
        resp = _client(host).post(WRITE_PATH, json=BAD_BODY)
        assert resp.status_code == 422, f"回环地址 {host} 被拦了，机器人会连不上"


def test_读接口不登录也能用(remote, monkeypatch):
    """★ 读是给部员用的，那正是这套东西的意义，不能被口令挡住。"""
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    assert remote.get("/api/meta").status_code == 200


def test_页面本身不登录也能打开(remote, monkeypatch):
    """门拦的是写操作，不是整个站点。"""
    monkeypatch.setenv("KB_SHARED_PASSWORD", PASSWORD)

    resp = remote.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ══ CORS ═════════════════════════════════════════════════════

def test_默认不放开跨域(remote, monkeypatch):
    """★ 原来是 `allow_origins=["*"]`，绑校园网之后那就是敞开的。

    没配 KB_CORS_ORIGINS 时不加 CORS 中间件——页面和接口同源，本来就不需要。
    这条红了说明有人把 `*` 加回来了。
    """
    monkeypatch.delenv("KB_CORS_ORIGINS", raising=False)

    resp = remote.get("/api/meta")

    assert "access-control-allow-origin" not in resp.headers, (
        "响应里出现了跨域头——默认不该放开跨域。"
        "服务器上要配就写 KB_CORS_ORIGINS=<具体来源>，不能用 `*`。"
    )
