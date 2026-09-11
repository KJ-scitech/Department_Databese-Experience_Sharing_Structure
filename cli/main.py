# -*- coding: utf-8 -*-
"""知识库命令行工具 —— 入口。

给 QQ 机器人（AstrBot）读写这个库用，顺便也让人能在终端里查东西。

    python -m cli --help
    python -m cli health
    python -m cli exp search "设备出故障了咋办"
    python -m cli part add --activity-id 1 --member-id 2 --role 主讲

═══════════════════════════════════════════════════════════════════
  输出约定：默认 JSON，一行的
═══════════════════════════════════════════════════════════════════

stdout 上永远只有一个 JSON 对象，**失败时也打**：

  {"ok": true,  "code": "ok", "data": [...], "message": "...", "low_confidence": false}
  {"ok": false, "code": "no_match", "data": [], "message": "...", "low_confidence": true}

这样机器人不用去啃中文报错，也不用区分「stdout 是 JSON 还是错误堆栈」——
看 exit code 决定怎么做，看 code 决定说什么话，看 message 直接念给用户听。

加 --pretty 就变成缩进的、给人看的形式（但还是 JSON，不是表格）。

═══════════════════════════════════════════════════════════════════
  退出码：机器人的分支依据
═══════════════════════════════════════════════════════════════════

  0  成功。★ 包括「一条都没搜到」——那是正常结果，不是错误
  1  其它（含服务返回的不是 JSON）
  2  参数写错了 —— 是机器人自己拼错了命令，记日志
  3  服务连不上 / 超时 —— 回「知识库服务暂时不可用」
  4  404 找不到 —— 回「没有这条记录」
  5  409 冲突 —— ★ 当作「已经做过了」，算成功，重试安全
  6  400 / 422 服务端拒绝 —— 把 message 念给用户听
  7  服务在跑但它自己说数据库不通 —— 回「数据库暂时不可用」

完整的映射规则在 cli/client.py。这里是出口。

★ 5 和 0 是两种不同的成功，别把它们当成「错误」处理：
  409 说明你要做的事已经有人做过了，对重试的机器人来说这是好消息。

═══════════════════════════════════════════════════════════════════
  机器人对接要点（给 QQBot 组看的）
═══════════════════════════════════════════════════════════════════

  1. 机器人和服务在同一台机器上，什么都不用配，默认就打 127.0.0.1:8000。
     换地址用环境变量 KB_API_BASE，或命令行加 --base。

  2. 写经验可以署名也可以不署：
       --author-id 3            署名到 member.id = 3 这个人
       --source "2026开甲学创部" 不署名，只记来自哪个群
     两个可以同时给（署了名也知道是从哪个群传的）。

  3. 登记分工（part add）**没有** --honor 参数，见 cli/directory.py 开头。
     因为写接口没有鉴权，不能让群里任何人给自己报荣誉。
     「谁能在群里让机器人登记分工」得部里定个名单，最省事的做法是
     机器人组维护一个 QQ 号白名单。

  4. 有写接口就意味着机器人有可能把脏数据写进库里。
     建议机器人自己对输入做个长度上限，别让一段几千字的聊天记录
     直接进来把检索结果冲掉。
"""
import argparse
import json
import sys

from scripts import console

from . import client, directory, exp, report


# ══ 命令行定义 ═══════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli",
        description="知识库命令行工具：读写经验知识库和活动分工数据库。",
        epilog="退出码见 cli/main.py 开头的说明。",
    )
    parser.add_argument(
        "--base",
        metavar="URL",
        help=f"服务地址。默认读环境变量 KB_API_BASE，再默认 {client.DEFAULT_BASE}",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="输出缩进过的 JSON，给人看（默认是单行，给程序读）",
    )

    sub = parser.add_subparsers(dest="resource", metavar="资源", required=True)

    # health 是个单条命令，不归任何资源，就直接挂在这一层。
    p = sub.add_parser("health", parents=[_common()], help="自检：服务通不通、库里有多少条")
    p.set_defaults(run=run_health)

    exp.register(sub, [_common()])
    directory.register(sub, [_common()])
    report.register(sub, [_common()])

    return parser


def _common() -> argparse.ArgumentParser:
    """--base / --pretty 也要能写在子命令后面。

        python -m cli --pretty exp search "..."     ← 可以
        python -m cli exp search "..." --pretty     ← 这样也行

    光在顶层定义就只能写前面那种，因为 argparse 到了子解析器就不认识它了。

    用 parents 把这两个参数再挂到每个叶子命令上，默认值设成 SUPPRESS。
    ★ SUPPRESS 是关键：不写 SUPPRESS 的话子解析器会用它自己的默认值
      （False / None）把顶层已经解析出来的值**覆盖掉**，
      于是 `--pretty exp search ...` 会莫名其妙失效。
      这是 argparse 的老坑，改这里的时候别把 SUPPRESS 删了。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base",
        metavar="URL",
        default=argparse.SUPPRESS,
        help=f"服务地址，默认 {client.DEFAULT_BASE}",
    )
    common.add_argument(
        "--pretty",
        action="store_true",
        default=argparse.SUPPRESS,
        help="输出缩进过的 JSON，给人看",
    )
    return common


# ══ 各命令的实现 ═════════════════════════════════════════════

def run_health(args, api: client.Client) -> client.Result:
    info = api.get("/api/health")

    # ★ 注意：数据库挂了的时候，/api/health 仍然返回 **HTTP 200**，
    #   只是 ok 变成 false。所以这里不能用「有没有抛异常」来判断，
    #   得看响应体里的 ok。
    #   这是「服务在，但降级了」，和「服务没了」（退出码 3）不是一回事，
    #   机器人该说的话也不一样。
    if not info.get("ok"):
        return client.Result(
            data=info,
            code="degraded",
            message=f"服务在跑，但数据库连不上：{info.get('database')}",
            ok=False,
            exit_code=client.EXIT_DEGRADED,
        )

    return client.Result(
        data=info,
        message=(
            f"服务正常：{info.get('experience_count')} 条经验，"
            f"模型 {info.get('model')}（{info.get('embedding_dim')} 维）"
        ),
    )


# ══ 出口 ═════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    # ★ 顺序有讲究，两步都要：
    #   1. console.setup()   —— 编不出来的字符降级成 ?，别让 print 崩掉
    #   2. encoding="utf-8"  —— 再把编码钉成 UTF-8
    #
    # 第 2 步是本 CLI 比 scripts/ 里那些脚本多要的一步。
    # 原因：这里的输出是**给程序读的数据**，不是给人看的提示。
    # stdout 重定向到管道时，Windows 会按本地编码（GBK）来编，
    # 中文全变成「?」——JSON 结构还在，内容已经烂了，
    # 而且机器人那边看不出是编码问题，只会以为库里存的就是问号。
    console.setup()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = build_parser()
    # 参数写错时 argparse 自己退 2 —— 正好是我们要的退出码，不用拦，
    # 让它抛 SystemExit 出去就行。
    args = parser.parse_args(argv)

    api = client.Client(base_url=getattr(args, "base", None))

    try:
        result = args.run(args, api)
        if not isinstance(result, client.Result):
            # 允许命令直接返回列表/字典，省得每个都包一层
            result = client.Result(data=result)
    except client.ApiError as exc:
        result = client.Result(
            data=None,
            code=exc.code,
            message=exc.message,
            ok=exc.ok,
            exit_code=exc.exit_code,
        )

    _emit(result, args)
    return result.exit_code


def _emit(result: client.Result, args) -> None:
    envelope = {
        "ok": result.ok,
        "code": result.code,
        "data": result.data,
        "message": result.message,
        # 给机器人留的「别把话说太满」信号：结果可信度低。
        # 目前只在 exp search 一条都没过相似度阈值时为 true。
        # 单独留一个键、而不是只看 code，是为了以后想放宽
        # （比如「有结果但都不太像」也置 true）时不用改协议。
        "low_confidence": result.low_confidence,
    }

    if getattr(args, "pretty", False):
        if result.message:
            print(result.message)
        print(json.dumps(envelope["data"], ensure_ascii=False, indent=2))
        return

    print(json.dumps(envelope, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
