# -*- coding: utf-8 -*-
"""CLI 的 HTTP 封装：服务地址、超时、把错误翻译成退出码。

═══════════════════════════════════════════════════════════════════
  为什么 CLI 走 HTTP，而不是直接连数据库 / 直接开向量库
═══════════════════════════════════════════════════════════════════

这是个硬约束，不是个人偏好。理由在 Chroma 那边，不在我们这边。

Chroma 1.5.9 用的是 Rust 绑定，它自己安装目录里的文档字符串写着：

  · PersistentClient —— "intended for local development and testing.
    For production, prefer a server-backed Chroma instance."

  · SqlEmbeddingsQueue —— "only suitable for use cases where the producer
    and consumer are in the same process … does not actively listen to the
    database for new records added by other processes."

翻成人话：**两个进程同时开 data/chroma/，读的一方不保证能看到另一方
刚写进去的数据。**

服务是长期跑着的，CLI 是敲一下就没的短命进程——这正好是最坏的那种组合：
短命进程读到的是旧索引，算出下一个 id，然后把已经存在的经验**静默覆盖**掉。
不报错，数据就没了。

所以规矩是：**同一时刻只许一个进程碰向量库**。服务已经在跑了
（systemd 常驻），就让它当那个唯一进程。CLI 不跟它抢。

走 HTTP 还有几个白拿的好处：

  · 校验逻辑零重复。409 重复登记、400 外键不存在、404 找不到，
    这些服务端已经写好了，CLI 全部自动继承。
  · 机器人写进去的数据和网页表单录入的走**同一套规则**，
    不会出现「机器人记的分工和网页记的不一样」这种事。
  · 不用把路由里的查询抽成 service 层。全项目的风格就是
    「逻辑写在路由里，坑写在文件开头的注释里」，
    为了一个不存在的第二消费者去重构 12 个能跑的路由，只会引入回归。

═══════════════════════════════════════════════════════════════════
  CLI 里不许 import api
═══════════════════════════════════════════════════════════════════

本包（cli/）只依赖**标准库 + requests**，一行 `from api import ...` 都不写。

  1. 一旦 import api.vectors 就会把 torch 拖进导入图，
     每个命令凭空多等好几秒，而命令本身只是发个 HTTP 请求。
  2. 躲开一个真实的坑：HF_ENDPOINT（模型下载镜像）必须在导入
     transformers 之前设好。CLI 不碰这条路径，就完全不受影响。

代价是几个常量要在两边各写一份（比如经验类型），见 cli/exp.py 的注释。
这个代价比上面两条小得多。
"""
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import requests

# ── 退出码：机器人就是靠这个分支的 ────────────────────────────
#
# 机器人读不懂中文报错，也不想读。所以「发生了什么」用退出码表达，
# 「具体哪条不对」才放在 message 里给人看。
EXIT_OK = 0            # 成功。★ 包括「一条都没搜到」——那是正常结果，不是错误
EXIT_ERROR = 1         # 其它（含服务返回的不是 JSON）
EXIT_USAGE = 2         # 参数写错了。argparse 自己就退 2，顺水推舟
EXIT_UNREACHABLE = 3   # 服务连不上 / 超时
EXIT_NOT_FOUND = 4     # 404
EXIT_CONFLICT = 5      # 409。★ 当作「已经做过了」，算成功，重试安全
EXIT_REJECTED = 6      # 400 / 422，服务端明确拒绝
EXIT_DEGRADED = 7      # 服务在跑，但它自己说数据库不通（health 的 ok:false）

DEFAULT_BASE = "http://127.0.0.1:8000"

# requests **没有**默认超时——不显式给就是「永远等下去」，
# 机器人会一直卡着不回复，比报错还糟。
#
# 连 3 秒：同机通信，连不上就是没起来，没必要等。
# 读 30 秒：给得宽是因为检索要现场把问句编码成向量，
#          而服务器没有 GPU，是纯 CPU 在跑，比开发机慢。
TIMEOUT = (3, 30)


@dataclass
class Result:
    """一次成功的调用。main 会把它包成信封打到 stdout。

    exit_code 一般就是 0。有两个例外：
      · health 发现数据库不通 → 7（服务在，但降级了）
      · 409 重复登记 → 5（ok 仍然是 True，因为它其实是成功）
    """

    data: Any = None
    code: str = "ok"
    message: str = ""
    ok: bool = True
    # 见 main._emit 里的说明
    low_confidence: bool = False
    exit_code: int = EXIT_OK


class ApiError(Exception):
    """一次失败的调用。main 捕获它，打信封，然后按 exit_code 退出。

    exit_code=EXIT_CONFLICT 时 ok 是 True —— 409 表示「你要做的事
    已经做过了」，对重试的机器人来说是好事不是坏事。
    """

    def __init__(self, code: str, message: str, exit_code: int, ok: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.ok = ok


def _default_transport(method: str, url: str, params, json_body, timeout):
    """默认传输层：真的发一个 HTTP 请求。返回 (状态码, 响应正文文本)。

    单独拆成一个函数是为了让测试能换掉它——tests/test_cli.py 塞一个假的进来，
    就能在不联网、不起服务、不加载模型的情况下把退出码映射钉死。
    """
    response = requests.request(method, url, params=params, json=json_body, timeout=timeout)
    return response.status_code, response.text


def _parse_json(text: str):
    """返回 (解析结果, 是否是合法 JSON)。"""
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


def _detail_to_text(detail) -> str:
    """把 FastAPI 的错误体翻成一行人话。

    ★ 有两种形状，两种都得认：
        HTTPException（我们主动抛的） → {"detail": "一句中文"}
        Pydantic 校验失败（422）      → {"detail": [ {"loc": [...], "msg": ...}, ... ]}
      第二种 detail 是**列表**，直接 str() 出来是一坨 Python 字典，
      扔给机器人等于没给。
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                # loc 形如 ["body", "type"]，第一段是 "body"，去掉它更有用
                loc = [str(x) for x in item.get("loc", []) if x != "body"]
                msg = str(item.get("msg", "")).strip()
                parts.append(f"{'.'.join(loc)}：{msg}" if loc else msg)
            else:
                parts.append(str(item))
        return "；".join(p for p in parts if p)
    return ""


def _error_from(status: int, payload, text: str) -> ApiError:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    message = _detail_to_text(detail) or (text or "").strip()[:200] or f"HTTP {status}"

    if status == 404:
        return ApiError("not_found", message, EXIT_NOT_FOUND)
    if status == 409:
        # 重复登记不是错误。机器人重试同一条指令时应该走到这里，
        # 并且应该当成功处理——所以 ok=True，只是退出码分开好让它能分辨。
        return ApiError("conflict", message, EXIT_CONFLICT, ok=True)
    if status in (400, 422):
        return ApiError("rejected", message, EXIT_REJECTED)
    return ApiError("http_error", message, EXIT_ERROR)


class Client:
    """一个很薄的 HTTP 客户端。

    基址的取法：--base 参数 > 环境变量 KB_API_BASE > 默认 http://127.0.0.1:8000。
    服务器上机器人和服务在同一台，默认值就够用，不用配。
    """

    def __init__(
        self,
        base_url: str | None = None,
        transport: Callable | None = None,
        timeout=TIMEOUT,
    ):
        self.base_url = (base_url or os.environ.get("KB_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self._transport = transport or _default_transport

    # ── 三个动词 ──────────────────────────────────────────────

    def get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict):
        return self._request("POST", path, json_body=body)

    def delete(self, path: str):
        return self._request("DELETE", path)

    # ── 真正干活的地方 ────────────────────────────────────────

    def _request(self, method: str, path: str, params=None, json_body=None):
        url = self.base_url + path
        try:
            status, text = self._transport(method, url, params, json_body, self.timeout)
        except requests.RequestException as exc:
            # 服务没起来 / 端口不对 / 超时，全归这一类。
            # 机器人看到 3 就知道该说「知识库暂时不可用」，而不是「没有这条记录」。
            raise ApiError(
                "unreachable",
                f"连不上知识库服务 {self.base_url}（{exc.__class__.__name__}）",
                EXIT_UNREACHABLE,
            ) from exc

        payload, is_json = _parse_json(text)

        if status >= 400:
            raise _error_from(status, payload, text)

        if not is_json:
            # 比如请求打到了别的服务上、或者被反代插了一页 HTML 错误页。
            # 这种时候任何「成功」的判断都是假的，直接报错。
            raise ApiError(
                "bad_response",
                f"服务返回的不是 JSON（HTTP {status}）：{text.strip()[:200]}",
                EXIT_ERROR,
            )

        return payload
