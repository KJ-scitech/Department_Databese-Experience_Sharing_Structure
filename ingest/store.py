# -*- coding: utf-8 -*-
"""切片 → K1 接口入库。

╔══════════════════════════════════════════════════════════════╗
║  ★★★ 不要在这里加自动重试 ★★★                              ║
║                                                              ║
║  K1（POST /api/experience）没有幂等键、没有去重：同一条      ║
║  content 连发两次，库里就是两条。所以任何一个失败都           ║
║  **只记录、不重发**。                                        ║
║                                                              ║
║  特别是 ReadTimeout——请求已经发出去了，服务端很可能已经      ║
║  写进去了，这时候重试就是在制造重复数据。要做重试，必须先    ║
║  做失败分类（只有「连接都没建立」的 ConnectTimeout 才安全）， ║
║  那是另一块独立的工作，不是「顺手加个 retry」能解决的。       ║
╚══════════════════════════════════════════════════════════════╝

★ 重复跑同一份文件靠**入库前探测**挡住，不靠重试：
  每片发出前先用 `/api/experience/keyword` 按字面查一遍，
  content 完全相同且 source 相同就跳过。这是尽力而为的护栏
  （keyword 接口最多返回 20 条），不是数据库级的唯一约束。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

from cli import client as cli_client
from ingest import config

# 探测用的正文前缀长度。整条切片塞进 URL 会太长；
# 120 字已经足够把一条切片和别的切片区分开。
_PROBE_CHARS = 120


def make_transport(password: str | None = None):
    """带上 X-KB-Password 的传输层。

    服务端 api/auth.py 对回环地址（127.0.0.1 / ::1）免口令，所以本机跑
    不需要这个。只有把脚本指到别的机器时才用得上。

    ★ 请求头只能是 ASCII。中文口令必须先 quote() 百分号编码——
      服务端两种写法都认（api/auth.py 的 _credentials_ok）。
    """
    headers = {"X-KB-Password": quote(password, safe="")} if password else {}

    def transport(method, url, params, json_body, timeout):
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=timeout,
            headers=headers or None,
        )
        return response.status_code, response.text

    return transport


def make_client(base_url: str | None = None, password: str | None = None) -> cli_client.Client:
    """复用 CLI 的 HTTP 封装：基址取值、错误翻译、退出码全都继承。

    基址优先级：--base > 环境变量 KB_API_BASE > http://127.0.0.1:8000
    （见 cli/client.py 的 Client.__init__）。
    """
    return cli_client.Client(
        base_url=base_url,
        transport=make_transport(password),
        timeout=config.POST_TIMEOUT,
    )


def make_payload(
    content: str,
    type_: str,
    tags: list[str],
    source: str | None,
    author_id: int | None = None,
    activity_id: int | None = None,
    participation_id: int | None = None,
) -> dict:
    """构造 K1 的请求体。

    只有非 None 的可选字段才放进去——服务端对 None 的处理虽然没问题，
    但少传字段能让请求体一眼看懂，也少一类「为什么传了 null」的疑问。
    """
    payload = {
        "content": content,
        "type": type_,
        "tags": list(tags),
        "source": source,
    }
    if author_id is not None:
        payload["author_id"] = author_id
    if activity_id is not None:
        payload["activity_id"] = activity_id
    if participation_id is not None:
        payload["participation_id"] = participation_id
    return payload


def find_existing(client, content: str, source: str | None) -> str | None:
    """尽力而为地查「这条切片是不是已经在库里了」。

    命中条件是 **content 完全相等 且 source 相同**，所以不会误判。
    查不到不代表一定没有（keyword 接口 limit 上限是 20），
    所以它只是状态之外的第二道护栏；真正的保证是「失败不自动重试」。
    """
    probe = content[:_PROBE_CHARS]
    try:
        hits = client.get("/api/experience/keyword", q=probe, limit=20)
    except cli_client.ApiError:
        # 探测失败不能挡住入库——最多多一条重复，比整趟跑不了好。
        return None

    for hit in hits:
        metadata = hit.get("metadata") or {}
        if hit.get("content") == content and metadata.get("source") == source:
            return hit.get("exp_id")
    return None


@dataclass
class IngestStats:
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: list[dict] = field(default_factory=list)
    exp_ids: list[str] = field(default_factory=list)
    aborted: str = ""     # 非空表示中途停下了，值是原因
    elapsed: float = 0.0


def ingest_chunks(
    client,
    chunks: list[str],
    *,
    type_: str,
    tags: list[str],
    source: str | None,
    author_id: int | None = None,
    activity_id: int | None = None,
    participation_id: int | None = None,
    skip_existing: bool = True,
    log=print,
) -> IngestStats:
    """逐片入库。

    遇到下面两类错误会**立刻停下**，不再往后发：
      · rejected（400/422）：所有切片共用同一套参数，第一片被拒，
        后面的会一模一样地被拒。停下既省请求，也让「库里有零条写入」
        这个状态保持干净。
      · unreachable（连不上）：继续发只会刷屏。
    其余错误（5xx、非 JSON）记下来继续，最后一起报。
    """
    total = len(chunks)
    stats = IngestStats(total=total)
    started = time.perf_counter()
    recent: list[float] = []

    for index, chunk in enumerate(chunks, start=1):
        if skip_existing:
            existing = find_existing(client, chunk, source)
            if existing:
                stats.skipped += 1
                log(f"  [{index}/{total}] 已在库中（{existing}），跳过")
                continue

        payload = make_payload(
            chunk,
            type_,
            tags,
            source,
            author_id=author_id,
            activity_id=activity_id,
            participation_id=participation_id,
        )

        item_started = time.perf_counter()
        try:
            result = client.post("/api/experience", payload)
        except cli_client.ApiError as exc:
            stats.failed.append(
                {
                    "index": index,
                    "code": exc.code,
                    "message": exc.message,
                    "content": chunk,
                }
            )
            log(f"  [{index}/{total}] 入库失败：{exc.message}（{exc.code}）")
            log(f"      该片开头：{chunk[:40]}…")

            if exc.code == "rejected":
                stats.aborted = "服务端拒绝了这批参数（400/422），后面的切片会同样失败"
                log(f"[停止] {stats.aborted}。本次一条都没有写进去。")
                break
            if exc.code == "unreachable":
                stats.aborted = "服务连不上"
                log(f"[停止] {stats.aborted}。检查服务是否在跑：python -m cli health")
                break
            if "口令" in (exc.message or ""):
                log("      提示：服务不在回环地址上，需要加 --password 或设置 KB_SHARED_PASSWORD")
            continue

        elapsed = time.perf_counter() - item_started
        recent.append(elapsed)
        recent = recent[-20:]

        exp_id = result.get("exp_id") if isinstance(result, dict) else None
        if exp_id:
            stats.exp_ids.append(exp_id)
        stats.success += 1

        avg = sum(recent) / len(recent)
        remaining = total - index
        log(
            f"  [{index}/{total}] 入库成功 {exp_id or '（服务端没返回 exp_id）'}"
            f"  用时 {elapsed:.1f}s  预计剩余 {avg * remaining / 60:.1f} 分钟"
        )

    stats.elapsed = time.perf_counter() - started
    return stats
