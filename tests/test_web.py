"""前端的冒烟测试：`web/index.html` 和它调的那些接口。

★ 这个文件**故意不连向量库、不加载模型**（不用 `client` 夹具）。
  它要挡的是「页签建了没接线」「页面还留着演示时的引导词」这类低级错误，
  这类错误不该让人等 20 秒模型加载才能发现。

★ 为什么「页面里出现的每个 /api/ 路径都要真的存在」值得单独写一条测试：
  前端是单文件原生 JS，没有类型检查、没有构建、没有 lint——
  把 `/api/card/members` 拼错一个字母，页面只会静默地一直转圈，
  浏览器控制台也不会说「这个接口不存在」。这条测试就是那个编译器。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "index.html"

# 页面里形如 /api/xxx/yyy 的调用。字符类只收路径里合法的字符，
# 所以中文标点和 JS 的引号会自然截断匹配。
_API_CALL = re.compile(r"/api/[A-Za-z0-9_/{}$.\-]*")


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page():
    """不用 with 构造：不跑 lifespan，也就不会加载模型、不会开向量库。

    这里只要求「路由能匹配上、静态文件发得出去」，这两件事都在
    app 导入时就定好了，跟 lifespan 无关。
    """
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


# ══ 页面本身 ═════════════════════════════════════════════════

def test_根路径返回的是页面(page):
    resp = page.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "学创部知识库" in resp.text


def test_页面不引用任何外部资源(html):
    """★ 单文件 + 不依赖 CDN。

    服务跑在部里的 M710q 上，访问者可能在校园网内、也可能在外网，
    但只要页面上有一个 `<script src="https://...">`，那个 CDN 一旦被墙
    或者变慢，整个页面就是白屏——而且「本地跑得通」永远复现不出来。
    图标用 `data:,` 内联，也是为了不让浏览器去要 /favicon.ico。
    """
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "@import" not in html


def test_没有演示时期的痕迹(html):
    """★ K9 的完成标准之一是「不用敲命令」。

    页面上原来有四处「先跑 python -m scripts.seed」，还有标题里的「· 演示」。
    这些留着的话，第一次打开页面的人会以为这是个还没接数据的样例站。
    """
    assert "演示" not in html
    assert "scripts.seed" not in html
    assert "python -m scripts" not in html


def _code_only(html: str) -> str:
    """去掉整行注释，只留代码。

    ★ 为什么非要去注释：页面里恰好有一段注释写着「不用 confirm()」——
      对整份文件做子串检查，会被这句**讲解规矩的话**判成违规。
      这类假红很典型：文件越是把自己的规矩写清楚，越容易被自己的注释绊倒。
      （只去行首是注释的行。行尾注释仍然会漏，这是刻意的取舍——
       换成能识别字符串的正则去剥注释，风险比它挡住的假红还大。）
    """
    keep = []
    for line in html.splitlines():
        s = line.strip()
        if s.startswith(("//", "/*", "*", "<!--")):
            continue
        keep.append(line)
    return "\n".join(keep)


def test_不用alert和confirm(html):
    """★ 校验和确认都在页面里就地做。

    `alert()`/`confirm()` 是浏览器原生弹窗：样式改不了、会被拦截器吃掉、
    还会打断整页操作。错误信息挂在字段下面，删除确认做成行内的一条横条。
    """
    code = _code_only(html)

    for name in ("alert", "confirm", "prompt"):
        # 前面不允许是字母/点，免得把 `window.confirm` 之外的东西（比如自己写的
        # confirmDelete）误判进来
        assert not re.search(rf"(?<![\w.$]){name}\s*\(", code), (
            f"页面里直接调了浏览器原生的 {name}()。"
            "校验错误请挂在字段下面（.err），确认请在行内做。"
        )


def test_页面不采集敏感字段(html):
    """★★ 网页表单只收「能对外说」的那部分。

    写接口目前没有鉴权，谁打开这个页面都能提交。学号/手机/邮箱/班级/生日
    属于不该经无鉴权表单流转的那一类，所以**页面上根本没有这些输入框**——
    不是靠前端校验挡，是让它没有入口。

    红了怎么办：如果是真有业务需要，先给写接口加鉴权，再讨论要不要放开，
    别把这条断言删掉就算完。
    """
    for field in ("student_no", "phone", "email", "wechat", "birthday", "class_name"):
        assert field not in html, (
            f"页面里出现了敏感字段 {field}。写接口没有鉴权，"
            "这类字段应当只走数据库或带权限的接口，不要在网页表单上开入口。"
        )


# ══ 接线：页面上调的接口必须真的存在 ═════════════════════════

def _called_paths(html: str) -> set[str]:
    """抽出页面里所有 /api/ 调用，把模板串里的 id 换成占位符。

    源码里写的是 `/api/experience/${expId}`，匹配到的部分到 `${` 就断了；
    统一还原成 `/api/experience/{p}` 才能和路由模板比。
    """
    out = set()
    for raw in _API_CALL.findall(html):
        raw = raw.rstrip(".-")
        norm = re.sub(r"\$\{[^}]*\}", "{p}", raw)
        # 只剩 `/api` 或 `/api/` 的不算一次调用（多半是注释里的泛指）
        if norm.rstrip("/") == "/api":
            continue
        out.add(norm)
    return out


def _template_to_regex(path: str) -> str:
    """`/api/members/{member_id}` → `/api/members/[^/]+`"""
    parts = re.split(r"\{[^}]+\}", path)
    return "[^/]+".join(re.escape(p) for p in parts)


def test_页面里每个api调用都有对应的路由(html, page):
    """★★ 前端没有编译期检查，这条测试就是它的编译期检查。

    单文件原生 JS 里把 `/api/cards/members` 敲成 `/api/card/members`，
    表现是页签点开一直转圈、控制台一句人话都没有。这里逐个比对
    路由表，拼错就会指名道姓地报出来。
    """
    from api.main import app

    routes = sorted(app.openapi()["paths"])
    called = _called_paths(html)

    assert called, "一个 /api/ 调用都没抽到，正则或者页面是不是变了？"

    unknown = sorted(
        p for p in called
        if not any(re.fullmatch(_template_to_regex(r), p) for r in routes)
    )
    assert not unknown, (
        f"页面在调不存在的接口：{unknown}\n"
        f"现在有的接口是：{routes}"
    )


def test_介绍卡页签接了线(html):
    """★ 防「页签建了没接线」：光有个「介绍卡」按钮不算数，
    得真的在调 K6 的两个接口。
    """
    assert "/api/cards/members" in html
    assert "/api/cards/activities" in html


def test_枚举是从接口拉的而不是写死的(html):
    """★ K9：类型/角色/状态这些下拉框的值由后端说了算。

    页面里留一份默认值只是为了「接口挂了不白屏」，那份默认值上面会挂黄条说明；
    真正填选项的路径必须是 GET /api/meta。
    """
    assert "/api/meta" in html
    assert "api('/api/meta')" in html


def test_校验没过时消息行也要更新(html):
    """★ 只把字段标红不够，消息行也得换掉。

    `clearErrs()` 清的是红字（`.field.bad` 和 `.err`），**不碰消息行**
    （`#nm-msg`、`#ac-msg` 这些）。所以一个表单如果校验没过就 `return`，
    上一次成功留下的「已新增成员，id 3。」会原样留在下面，紧挨着一个红字——
    看着像「提交成功了，但有个警告」，实际上一个写请求都没发出去。
    这是走查时真踩到的：活动/成员/系列三个表单漏了，录入和分工两个本来就写对。

    红了怎么办：在同一个分支里补上
    `return msg(<那条消息行>, '有字段没填对，看红字。', false)`。
    行内改标签那个是例外（红字就挂在输入框下面，那个作用域里没有消息行），
    所以这里只认 `!check(...)` / `if (!ok)` 这两种写法，不去管别的 `return;`。
    """
    code = _code_only(html)

    # 两种写法：直接 `if (!check([...])) return;`，
    # 或者先 `const ok = check([...]);` 再 `if (!ok) return;`。
    # 后面带 `{` 的（`if (!ok) { msg(...); return; }`）不会被这两种匹配到。
    bad = re.findall(r"(?:\]\)\)|if\s*\(\s*!ok\s*\))\s*return\s*;", code)

    assert not bad, (
        f"有 {len(bad)} 处校验失败只 return 了，没写消息行。"
        "上一次成功的提示会留在页面上，和红字一起显示成「成功了但有警告」。"
        "改成 `return msg(<消息行>, '有字段没填对，看红字。', false)`。"
    )


def test_页面从鉴权接口读的字段接口真的返回(html, page):
    """★ 补这条是因为真踩了：`AUTH.enabled` 对不上接口返回的 `auth_enabled`。

    页面里读到的永远是 `undefined`，于是 renderAuth 一直走"没启用口令"
    那条分支——**登录框被藏起来，谁都登不进去**，而写入口碰巧藏对了，
    看着还挺像回事。控制台里一声不响。

    字段名写错是前端最难自己发现的一类错：JS 读一个不存在的属性返回
    undefined，不报错、不告警，结果只是行为悄悄跑偏。这条测试拿页面里
    写死的 `AUTH.<字段>` 和接口实际返回的键对一遍，对不上就指名报出来。
    """
    code = _code_only(html)
    used = set(re.findall(r"\bAUTH\.(\w+)", code))

    assert used, "页面里一处都没读 AUTH.<字段>，登录逻辑是不是被删了？"

    returned = set(page.get("/api/auth/me").json())

    missing = sorted(used - returned)
    assert not missing, (
        f"页面在读鉴权接口没有的字段：{missing}\n"
        f"接口实际返回的是：{sorted(returned)}\n"
        "字段名对不上的话，JS 读出来是 undefined，不报错、只是行为悄悄跑偏。"
    )


def test_相似度分档和命令行是同一组数(html):
    """★ 网页说「可能相关」而机器人说「没找到」，是最难查的一类不一致——
    因为两边各自都「没错」，只是界线不一样。

    所以这里直接把 cli/exp.py 的两个常量拿来对：改了命令行没改网页，
    这条测试就红，逼着一起改。
    """
    from cli.exp import CONFIDENT_SIMILARITY, DEFAULT_MIN_SIMILARITY

    assert str(DEFAULT_MIN_SIMILARITY) in html, (
        f"页面上找不到 {DEFAULT_MIN_SIMILARITY}（CLI 的相似度下限）。"
        "改了 cli/exp.py 的阈值，web/index.html 里 .tier 的三档要跟着改。"
    )
    assert str(CONFIDENT_SIMILARITY) in html, (
        f"页面上找不到 {CONFIDENT_SIMILARITY}（CLI 的高置信阈值）。"
    )


def test_成员排序用的是接口给的职务顺序(html):
    """页面按 /api/meta 的 title_order 排成员，不硬写第二份顺序。

    硬写的坏处：以后职务变了（加个「常务副部长」之类），接口和页面各写一份，
    改一处漏一处，排序就悄悄不一致了。
    """
    assert "title_order" in html, "页面没有用接口给的职务顺序"
