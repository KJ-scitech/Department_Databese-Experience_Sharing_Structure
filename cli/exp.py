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

try:
    from tagging import rules
except ImportError:  # pragma: no cover —— 只有 tagging/ 还没到位时才会走到
    rules = None  # type: ignore[assignment]

from . import client

# tagging/ 是 K8 那块（本仓库里最新的一块，别的人在写）。它要是还没合进来，
# 「查经验 / 录经验」这两条机器人天天要用的命令不能跟着一起挂掉——
# 所以导入失败不在模块顶层炸，等真正要跑打标的时候再明确报错（见 _load_rules）。
# 这里只是给 --tagger 一个兜底的候选项；真跑的时候还要再确认一次零件在不在。
TAGGER_NAMES: tuple[str, ...] = tuple(getattr(rules, "TAGGER_NAMES", None) or ("rules",))

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

# 服务端 GET /api/experience 的 limit 上限就是 200（api/routers/experience.py 的
# Query(50, ge=1, le=200)）。给大了会被 422 挡住，所以翻页就用这个数。
PAGE_SIZE = 200


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
    # 三个筛选参数和服务端 GET /api/experience 的那三个一一对应，
    # 不传就是全量。挑经验出来核对的活（打标前后点数、查某个人的录入）
    # 靠它们，不用把整库拉下来自己过滤。
    p.add_argument("--type", choices=EXPERIENCE_TYPES, help="只看某一类，如 笔记")
    p.add_argument("--tag", help="只看带这个标签的经验")
    p.add_argument("--author-id", type=int, help="只看这个人署名的经验（member.id）")
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
        "--participation-id",
        type=int,
        help="关联一条参与记录 participation.id。★ 填了之后服务端会把新经验 id "
             "追加进那条记录的 exp_ids —— 报表和个人履历就是靠这个找经验的",
    )
    p.add_argument(
        "--source",
        help="来源。机器人不署名时用它记群名，如「2026开甲学创部」",
    )
    p.set_defaults(run=run_add)

    p = actions.add_parser("delete", parents=parents, help="⚠ 删除一条经验，删了找不回来")
    p.add_argument("exp_id", help="形如 exp-1")
    p.set_defaults(run=run_delete)

    p = actions.add_parser(
        "tag",
        parents=parents,
        help="K8 给**还没标签**的经验补标签（只补空，绝不覆盖已有标签）",
    )
    p.add_argument(
        "--tagger",
        choices=TAGGER_NAMES,
        default="rules",
        help="打标器，默认 rules。llm 还没实现，给了会明确报错退出",
    )
    p.add_argument("--limit", type=int, default=PAGE_SIZE, help=f"最多扫多少条，默认 {PAGE_SIZE}")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出来要打什么标签，一个写请求都不发（第一次跑建议先加这个）",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="写完之后照原样再扫一遍，确认没有漏补的（幂等自检：第二遍应当一条都不用补）",
    )
    p.set_defaults(run=run_tag)


# ══ 各动作的实现 ═════════════════════════════════════════════

def run_search(args, api: client.Client) -> client.Result:
    # 范围检查放在发请求之前：服务端也有（Query(ge=..., le=...)），
    # 但那样绕一圈回来的是英文 Pydantic 校验词，机器人看不懂该改什么。
    _require_range("--top-k", args.top_k, 1, 20)
    _require_range("--min-similarity", args.min_similarity, 0.0, 1.0)

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
        # 取最大的那个而不是 hits[0]：服务端现在按距离升序返回（最近的在前），
        # 但「最像的一条」这个说法依赖的是排序约定，不是接口承诺。
        # max 一行的事，就不用跟着别人的排序实现走了。
        best = max(h["similarity"] for h in hits) if hits else None
        return client.Result(
            data=[],
            code="no_match",
            message=(
                f"没有足够相关的经验（最像的一条 {best}，低于下限 {args.min_similarity}）"
                if best is not None
                else "经验库还是空的"
            ),
            # ★ 这一档是「一条都不给」，不是「给了但不太准」——
            # low_confidence 的语义只属于下面第 ② 档（见文件开头）。
            # 这里给 true 的话，只看这个标志位的机器人会对着空结果说
            # 「有一条可能相关的」，正好说反。机器人该认的是 code="no_match"。
            low_confidence=False,
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
    _require_range("--limit", args.limit, 1, 200)
    _require_range("--offset", args.offset, 0)
    _require_range("--author-id", args.author_id, 1)
    # 没给的筛选参数**不能带进请求**：把 None 塞进查询串，
    # 服务端会读到字符串 "None"，然后当成一个真的筛选条件——
    # 结果是「筛出 0 条」，跟「参数没给」完全不是一回事，还不好查。
    query: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    for key in ("type", "tag", "author_id"):
        value = getattr(args, key)
        if value is not None:
            query[key] = value
    items = api.get("/api/experience", **query)
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
    if args.participation_id is not None:
        body["participation_id"] = args.participation_id
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


def run_tag(args, api: client.Client) -> client.Result:
    """给没标签的经验补标签（K8）。

    ★ 流程是「先翻页扫完 → 再逐条写」，不边翻边写。
      边翻边写会让「这次补了几条」说不清：翻页期间自己写进去的记录
      会混进后面的页里，数字对不上；中途失败时更难看出到哪儿了。
      反正打标是幂等的（见 tagging/rules.py），先扫后写更简单也更安全。

    ★ 「只补空」在两层拦着：
      ① 下面循环里的 `if metadata["tags"]: continue`
      ② tagging.rules.tag() 只返回「要打什么」，没有任何写入入口
      第一层是校验，第二层是结构——结构那层才是真正兜底的。
    """
    _require_range("--limit", args.limit, 1)
    rules_mod = _load_rules()
    # 先确认打标器能用，再开始扫库。
    # 不然 --tagger llm 会把整个库扫一遍，最后才告诉你这个打标器还没做。
    try:
        rules_mod.ensure_available(args.tagger)
    except rules_mod.TaggerNotImplemented as exc:
        # 退出码给 2（参数问题）而不是 1（别的错）：这不是服务坏了，
        # 是「你要的东西没有，换个 --tagger 再试」。
        # 机器人看到 2 应该改命令，看到 1 才该去查服务。
        raise client.ApiError("not_implemented", str(exc), client.EXIT_USAGE) from exc

    scanned = _scan_experiences(api, args.limit)

    already = 0
    no_match = 0
    tagger_error = 0
    pending: list[dict[str, Any]] = []
    for item in scanned:
        if item.get("metadata", {}).get("tags"):
            already += 1
            continue  # ★ 只补空：人工写过的标签一个字都不动
        try:
            new_tags = rules_mod.tag_with(args.tagger, item.get("content", ""))
        except Exception:
            # 单条内容把打标器弄炸（编码、正则、奇形怪状的正文都碰得到），
            # 不该让整趟「定期跑一次」白跑：跳过它、记一笔，继续扫后面的。
            # 这一步全在写之前，所以跳过它是安全的——没有任何写入发生。
            tagger_error += 1
            continue
        if new_tags:
            pending.append({"exp_id": item["exp_id"], "tags": new_tags})
        else:
            no_match += 1  # 一个关键词都没命中，留着下次（或人工）再打

    counts = {
        "scanned": len(scanned),
        "already_tagged": already,
        "no_keyword_match": no_match,
        "tagger_error": tagger_error,
    }

    if args.dry_run:
        return client.Result(
            data={**counts, "tagged": 0, "dry_run": True, "pending": pending, "verify": None},
            message=(
                f"会补 {len(pending)} 条（扫了 {len(scanned)} 条，"
                f"已有标签 {already} 条，没命中关键词 {no_match} 条）——"
                "--dry-run，一个写请求都没发"
            ),
        )

    # 逐条 PATCH。★ 只发 {"tags": [...]}：正文/作者/活动 id 都不在这次的职责里，
    # 服务端也只收 tags（多传会 422）。tags 是**整份替换**不是追加，
    # 所以这里发的是算好的完整标签列表。
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in pending:
        try:
            api.patch(f"/api/experience/{item['exp_id']}", {"tags": item["tags"]})
        except client.ApiError as exc:
            # 常见的一条是 404：扫库和写库之间这条经验被人删了。
            # 为它中断整趟跑，会让前面已经补好的几十条连个交代都没有，
            # 所以记下来接着走，最后一起报。写请求本身是幂等的，重跑一遍就补上。
            failed.append({**item, "error": str(exc)})
            continue
        done.append(item)

    message = (
        f"补了 {len(done)} 条（扫了 {len(scanned)} 条，"
        f"已有标签 {already} 条，没命中关键词 {no_match} 条）"
    )
    if failed:
        message += f"，另有 {len(failed)} 条没写成功"

    # --verify：写完之后照原样再扫一遍，看还有没有能补的。
    # 规则版打标会把自己刚打的标签认成「已有标签」而跳过，
    # 所以第二遍一条都不用补才算对得上 K8 那条「跑两遍结果一样」。
    # 这正是「只补空、不覆盖」那条硬要求的机器可验证版本——
    # 光靠肉眼看「有没有被动过」是看不出来的。
    verify = None
    if args.verify:
        rescan = _scan_experiences(api, args.limit)
        still = [
            it["exp_id"]
            for it in rescan
            if not it.get("metadata", {}).get("tags")
            and rules_mod.tag_with(args.tagger, it.get("content", ""))
        ]
        verify = {"rescan": len(rescan), "still_pending": len(still), "ids": still}
        if still:
            message += f"；⚠ 再扫一遍还有 {len(still)} 条能补上（不是幂等，查一下打标器）"
        else:
            message += "；再扫一遍没有要补的了（幂等）"

    return client.Result(
        data={
            **counts,
            "tagged": len(done),
            "dry_run": False,
            "items": done,
            "failed": failed,
            "verify": verify,
        },
        message=message,
    )


def _scan_experiences(api: client.Client, limit: int) -> list[dict]:
    """翻页把经验取回来，最多 limit 条。

    取回来的每页都是「当时的快照」，服务端没有游标，翻页靠 offset。
    打标是加标签而不是插记录，条目数不会在翻页途中变多，
    所以 offset 翻页在这里够用。
    """
    items: list[dict] = []
    offset = 0
    while len(items) < limit:
        want = min(PAGE_SIZE, limit - len(items))
        page = api.get("/api/experience", limit=want, offset=offset)
        items.extend(page)
        if len(page) < want:
            break  # 已经到底了
        offset += len(page)
    return items[:limit]


# ══ 帮手 ═════════════════════════════════════════════════════

def _load_rules():
    """取打标器模块（tagging.rules），拿不到就明确报错。

    为什么不在模块顶层直接用它：tagging/ 是 K8 新加的一块，
    它还没合进来的时候，顶层 import 失败会让**整个** cli exp 都用不了——
    包括机器人真正在用的 search 和 add。所以顶层那份导入是容错的
    （失败就把 rules 置成 None），到真要打标的时候在这里说清楚。
    """
    if rules is None:
        raise client.ApiError(
            "not_implemented",
            "tagging/ 模块还不在这个仓库里，打标子命令暂时用不了；"
            "检索、查看、录入不受影响",
            client.EXIT_USAGE,
        )
    return rules


def _require_range(name: str, value, low, high=None) -> None:
    """参数范围在本地就拦掉，不留给服务端。

    服务端每个 Query 也都有 ge/le，拦得住；但一趟往返之后拿回来的
    是英文的 Pydantic 校验词（"Input should be less than or equal to 20"），
    机器人照着这句话不知道该改什么。在这里拦，能直接说清是哪个参数、
    合法范围是多少。越界的值一律退 2（参数问题），不是 1（服务坏了）。

    可选项没给的时候值是 None，那不算越界，直接放过去——
    不然 `None < 1` 会抛一个和参数毫无关系的 TypeError。
    """
    if value is None:
        return
    if value < low or (high is not None and value > high):
        span = f"{low}–{high}" if high is not None else f"≥{low}"
        raise client.ApiError(
            "bad_input",
            f"{name} 只能是 {span}，给的是 {value}",
            client.EXIT_USAGE,
        )


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
            # 用 utf-8-sig 读：Windows 上「另存为 UTF-8」写出来的文件常常带 BOM，
            # 用 utf-8 读会把 BOM 留在正文最前面（一个看不见的 \ufeff），
            # 存进库以后网页上、检索结果里都跟着它走。utf-8-sig 两种都读得对。
            text = Path(args.content_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise client.ApiError(
                "bad_input",
                f"读不了文件 {args.content_file}：{exc.strerror or exc.__class__.__name__}",
                client.EXIT_USAGE,
            ) from exc
        except UnicodeDecodeError as exc:
            # ★ 这个分支不是 OSError，所以以前它会漏出去变成一整屏 traceback。
            # Windows 上记事本另存为 ANSI（GBK）是最常见的写法，
            # 部里发过来的讲稿十有八九就是这种——要给出能照做的提示，
            # 而不是让机器人把一段栈当成错误消息发进群里。
            raise client.ApiError(
                "bad_input",
                f"读不了文件 {args.content_file}：不是 UTF-8 编码"
                f"（第 {exc.start} 字节）。请在编辑器里另存为 UTF-8 再试",
                client.EXIT_USAGE,
            ) from exc
    else:
        text = raw

    text = (text or "").strip()
    if not text:
        raise client.ApiError("bad_input", "经验正文是空的，没东西可录", client.EXIT_USAGE)
    return text
