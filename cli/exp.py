# -*- coding: utf-8 -*-
"""经验知识库的 CLI 子命令：search / get / list / add / delete。

对应服务端的 api/routers/experience.py（K1 录入 / K2 检索 / K3 元信息）。

═══════════════════════════════════════════════════════════════════
  search 为什么不把服务端返回的结果原样发出去
═══════════════════════════════════════════════════════════════════

api/vectors.py 的 search() **没有相似度下限**：库里只要有数据，
问什么都返回 top_k 条。这是有意的——网页那边要把相似度数字显示出来，
让人自己判断「像不像」。

但机器人不能这么干。群里有人问一句「今天中午吃啥」，
机器人会拿到 5 条经验，然后一本正经地当成答案发出去。
比「我不知道」糟糕得多。

所以过滤放在 CLI 这一层：拿到结果后按 --min-similarity 筛一遍，
全被筛掉就回 code="no_match"。**服务端一行都不用改**，网页不受影响。

═══════════════════════════════════════════════════════════════════
  实测数据：**没有**一条线能把「相关」和「不相关」分开
═══════════════════════════════════════════════════════════════════

下面这组数字是拿种子数据真跑出来的（24 个问句，每组 12 条），不是估的：

    相关问句    0.4920 … 0.7426      （最低「场地申请流程」→ exp-2）
    不相干问句  0.2598 … 0.5373      （最高「期末考试什么时候」→ exp-4）

**两段是重叠的。** 不相干里最高的 0.5373 比相关里最低的 0.4920 还高。
所以「找个阈值一刀切」这件事做不到——任何单一数字都必然误伤一边。

为什么重叠：这个模型在所有短中文句子上都会给出一个 0.3–0.55 的「背景分」，
因为库里全是学生活动相关的短句，向量都挤在一小片区域里。
**绝对分数不是「相不相关」的可靠信号**，这一点很反直觉，但实测就是这样。

所以不切一刀，分两档：

    ① 低于 --min-similarity（默认 0.45）→ 一条都不返回，code="no_match"
    ② 过了 ① 但最像的一条不到 CONFIDENT_SIMILARITY（0.55）
       → **照常返回，但 low_confidence=true**，机器人据此把话说软一点

第 ② 档是这套设计里最关键的一块。实测里那两条漏过去的不相干问句
（0.5307、0.5373）都会落在这一档，机器人会说「有一条可能相关的」而不是
「就是这条」——**错得可以接受**。如果只用一刀切，要么漏掉
0.4920 那条真正的答案（说「没找到」，其实库里有），要么放进一堆杂音。

宁可放过、不可错杀：对知识库来说，「库里有答案却告诉用户没有」比
「给了一条不太准的、用户自己看得出不靠谱」更糟。

换 embedding 模型的话这两个数要全部重新量——它们和模型绑死，不是通用常数。
"""
import sys
from pathlib import Path
from typing import Any

from . import client

# 和服务端 api/schemas.py 的 EXPERIENCE_TYPES 保持一致。
#
# 为什么不 from api.schemas import EXPERIENCE_TYPES 省这一行：
# 那会把整个 api 包导进来（进而把 torch 拖进导入图），
# 每个命令凭空多等好几秒。见 cli/client.py 开头的说明。
#
# 代价：服务端加新类型时这里要跟着加一行。所以这里只在本地拦明显的笔误，
# 服务端那边仍然会再校验一次——真漏了也只是多一次往返，不会写进脏数据。
EXPERIENCE_TYPES = ("牢骚", "问题", "笔记", "解答")

# 低于这个相似度就一条都不返回（code="no_match"）。
# 定在 0.45：实测相关问句最低 0.4920，绝大多数不相干问句在 0.44 以下。
DEFAULT_MIN_SIMILARITY = 0.45

# 过了下限但低于这个数，仍然返回，但置 low_confidence=true。
# 定在 0.55：实测两条漏网的不相干问句落在 0.53 上下，会落进这一档被标记。
# 两个数的来历和「为什么不能一刀切」见文件开头。
CONFIDENT_SIMILARITY = 0.55


# ══ 子命令注册 ═══════════════════════════════════════════════

def register(sub, parents) -> None:
    parser = sub.add_parser("exp", help="经验知识库：语义检索、查看、录入、删除")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)

    p = actions.add_parser(
        "search",
        parents=parents,
        help="语义检索——用大白话问，按意思找（机器人主要用这个）",
    )
    p.add_argument("q", help="自然语言问句，如「设备出故障了咋办」")
    p.add_argument("--top-k", type=int, default=5, help="最多要几条，默认 5（服务端上限 20）")
    p.add_argument(
        "--min-similarity",
        type=float,
        default=DEFAULT_MIN_SIMILARITY,
        help=f"相似度下限，低于它的一条都不返回，默认 {DEFAULT_MIN_SIMILARITY}。"
             "给 0 就是不筛（网页那边就是这么显示原始结果的）",
    )
    p.set_defaults(run=run_search)

    p = actions.add_parser("get", parents=parents, help="按 id 看一条经验的详情")
    p.add_argument("exp_id", help="形如 exp-1")
    p.set_defaults(run=run_get)

    p = actions.add_parser("list", parents=parents, help="分页列出经验")
    p.add_argument("--limit", type=int, default=50, help="每页几条，默认 50（上限 200）")
    p.add_argument("--offset", type=int, default=0, help="跳过前几条，默认 0")
    p.set_defaults(run=run_list)

    p = actions.add_parser(
        "add",
        parents=parents,
        help="录入一条经验（QQ 机器人往回写就是走这个）",
    )
    # --content 和 --content-file 二选一，必须给一个。
    # 分成两个参数是因为长文本塞不进命令行参数：Windows 的
    # CreateProcess 命令行总长有上限（约 32K 字符），群聊里
    # 复制过来的一大段总结很容易超。
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--content", help="正文。写 - 表示从标准输入读")
    source.add_argument("--content-file", metavar="路径", help="从文件读正文，或用 - 读标准输入")
    p.add_argument("--type", choices=EXPERIENCE_TYPES, default="笔记", help="默认 笔记")
    p.add_argument("--tags", help="标签，逗号分隔，如 场地,设备")
    p.add_argument(
        "--author-id",
        type=int,
        help="署名：关联 member.id。★ 不填就是匿名，此时应该用 --source 记下来自哪里",
    )
    p.add_argument("--activity-id", type=int, help="关联某场活动 activity.id")
    p.add_argument(
        "--source",
        help="来源。机器人不署名时用它记群名，如「2026开甲学创部」",
    )
    p.set_defaults(run=run_add)

    p = actions.add_parser("delete", parents=parents, help="⚠ 删除一条经验，删了找不回来")
    p.add_argument("exp_id", help="形如 exp-1")
    p.set_defaults(run=run_delete)


# ══ 各动作的实现 ═════════════════════════════════════════════

def run_search(args, api: client.Client) -> client.Result:
    hits = api.get(
        "/api/experience/search",
        q=args.q,
        top_k=args.top_k,
    )

    kept = [h for h in hits if h.get("similarity", 0) >= args.min_similarity]

    if not kept:
        # 注意：这里**不是**错误，退出码仍然是 0。
        # 「没找到」是一个正常答案，机器人应该说「没找到相关经验」，
        # 而不是报错——所以靠 code 区分，不靠退出码。
        best = hits[0]["similarity"] if hits else None
        return client.Result(
            data=[],
            code="no_match",
            message=(
                f"没有足够相关的经验（最像的一条 {best}，低于下限 {args.min_similarity}）"
                if best is not None
                else "经验库还是空的"
            ),
            low_confidence=True,
        )

    best = kept[0]["similarity"]
    # 第 ② 档：过了下限但把握不大。仍然把结果给出去（宁可放过不可错杀），
    # 但把 low_confidence 置起来，让机器人把话说软。
    hedged = best < CONFIDENT_SIMILARITY

    message = f"找到 {len(kept)} 条相关经验"
    if hedged:
        message += f"，但把握不大（最像的一条 {best}）"
    suppressed = len(hits) - len(kept)
    if suppressed:
        message += f"（另有 {suppressed} 条相似度不够，已略过）"

    return client.Result(data=kept, message=message, low_confidence=hedged)


def run_get(args, api: client.Client) -> client.Result:
    item = api.get(f"/api/experience/{args.exp_id}")
    return client.Result(data=item, message=f"{item.get('exp_id')}")


def run_list(args, api: client.Client) -> client.Result:
    items = api.get("/api/experience", limit=args.limit, offset=args.offset)
    return client.Result(data=items, message=f"共 {len(items)} 条（offset={args.offset}）")


def run_add(args, api: client.Client) -> client.Result:
    content = _read_content(args)

    body: dict[str, Any] = {"content": content, "type": args.type}
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.author_id is not None:
        body["author_id"] = args.author_id
    if args.activity_id is not None:
        body["activity_id"] = args.activity_id
    if args.source:
        body["source"] = args.source

    created = api.post("/api/experience", body)
    return client.Result(
        data=created,
        message=f"已录入 {created.get('exp_id')}",
    )


def run_delete(args, api: client.Client) -> client.Result:
    api.delete(f"/api/experience/{args.exp_id}")
    return client.Result(data={"deleted": args.exp_id}, message=f"已删除 {args.exp_id}")


# ══ 帮手 ═════════════════════════════════════════════════════

def _read_content(args) -> str:
    """把正文从 --content / --content-file / 标准输入里取出来。

    取到空正文就直接退 2（参数问题），不要发请求——
    服务端会退 400，但那时候已经绕了一圈，而且错误信息是英文的
    Pydantic 校验词，不如在这里说清楚。
    """
    raw = args.content_file if args.content_file is not None else args.content

    if raw == "-":
        text = sys.stdin.read()
    elif args.content_file is not None:
        try:
            text = Path(args.content_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise client.ApiError(
                "bad_input",
                f"读不了文件 {args.content_file}：{exc.strerror or exc.__class__.__name__}",
                client.EXIT_USAGE,
            ) from exc
    else:
        text = raw

    text = (text or "").strip()
    if not text:
        raise client.ApiError("bad_input", "经验正文是空的，没东西可录", client.EXIT_USAGE)
    return text
