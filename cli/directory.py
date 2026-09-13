# -*- coding: utf-8 -*-
"""活动分工数据库的 CLI 子命令：成员 / 系列 / 活动 / 参与记录。

对应服务端的 api/routers/directory.py（K4）。

═══════════════════════════════════════════════════════════════════
  成员信息为什么只有脱敏的那几个字段
═══════════════════════════════════════════════════════════════════

`member list` / `member get` 走的是 **v_member_public 视图**，
不是 member 表。学号、班级、生日、手机、邮箱、微信这些字段
**根本不在视图里**，所以这里想漏都漏不出去。

这不是 CLI 自己做的过滤，是数据库层做的。视图就是脱敏规则的
唯一事实来源——CLI 不抄一份字段清单，抄了就会有两处规则要维护。

机器人要在群里播成员信息时，拿到的就已经是能播的那部分。
真的要看敏感字段得走另一个接口（需要权限），本 CLI 不提供。

═══════════════════════════════════════════════════════════════════
  part add 为什么没有 --honor
═══════════════════════════════════════════════════════════════════

/api/reports/by-member 里的「荣誉次数」是评优时的依据。

而写接口目前**没有任何鉴权**。如果把「登记分工」开放给群里，
同时又能填 honor，那任何人都能给自己报一条荣誉记录，
而且事后从数据上看不出是谁报的。

所以 CLI 干脆不暴露这个参数——机器人组想写也写不了。
真要登记荣誉，走网页表单，那条路径上有人看着。

（每次参与登记倒是保留 --hours。它影响不了评优，
  只是给轮值时当参考的工作量。）

═══════════════════════════════════════════════════════════════════
  重复登记 = 409 = 成功
═══════════════════════════════════════════════════════════════════

participation 的唯一键是 (activity_id, member_id, role)：
同一个人在同一场活动里可以有多条记录，**只要角色不同**——
既当主讲又当执行的，就是两条。

机器人要是把同一条登记发了两遍（网络抖动、群里有两个人同时发），
第二次会撞唯一键。服务端回 409，CLI 把它映射成退出码 5 且 ok=true
——意思是「你要做的事已经做过了」，对重试的机器人来说是成功不是失败。

CLI 不自己先去查一遍「是不是已经登记过了」，那样反而更糟：
查一次再写一次，中间又多一个竞态窗口，而且判重规则就抄了第二份。
让数据库的唯一键去判，是唯一可靠的做法。
"""
from . import client


# ══ 子命令注册 ═══════════════════════════════════════════════

def register(sub, parents) -> None:
    _register_member(sub, parents)
    _register_series(sub, parents)
    _register_activity(sub, parents)
    _register_participation(sub, parents)


def _register_member(sub, parents) -> None:
    parser = sub.add_parser("member", help="成员：名单、详情、个人活动履历（已脱敏）")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)

    p = actions.add_parser("list", parents=parents, help="成员名单")
    p.add_argument("--keyword", help="按姓名或所属组模糊匹配")
    p.set_defaults(run=run_member_list)

    p = actions.add_parser("get", parents=parents, help="单个成员的详情")
    p.add_argument("member_id", type=int, help="member.id")
    p.set_defaults(run=run_member_get)

    p = actions.add_parser(
        "history",
        parents=parents,
        help="某个人的完整履历：参加过哪些活动、担任什么角色、关联了哪些经验",
    )
    p.add_argument("member_id", type=int, help="member.id")
    p.set_defaults(run=run_member_history)


def _register_series(sub, parents) -> None:
    parser = sub.add_parser("series", help="活动系列（品牌/栏目，如「惟学沙龙」）")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)
    p = actions.add_parser("list", parents=parents, help="系列列表")
    p.set_defaults(run=run_series_list)


def _register_activity(sub, parents) -> None:
    parser = sub.add_parser("activity", help="活动场次（系列下具体办的一次）")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)
    p = actions.add_parser("list", parents=parents, help="场次列表，按日期排序")
    p.add_argument(
        "--include-cancelled",
        action="store_true",
        help="把「取消」的场次也列出来（默认不列，因为统计时不该算进去）",
    )
    p.set_defaults(run=run_activity_list)


def _register_participation(sub, parents) -> None:
    parser = sub.add_parser("part", help="参与记录：谁 × 哪场活动 × 什么分工")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)

    p = actions.add_parser("list", parents=parents, help="查参与记录，可按活动或按人筛")
    p.add_argument("--activity-id", type=int, help="只看某场活动")
    p.add_argument("--member-id", type=int, help="只看某个人")
    p.set_defaults(run=run_part_list)

    p = actions.add_parser(
        "add",
        parents=parents,
        help="登记一条分工（机器人登记分工就是走这个）",
    )
    p.add_argument("--activity-id", type=int, required=True, help="哪场活动")
    p.add_argument("--member-id", type=int, required=True, help="谁")
    # 故意不给默认值。默认成「参与」的话，机器人漏传 role 会静默写进一条
    # 没有信息量的记录，而且看数据看不出来是漏传的。宁可让它报参数错。
    p.add_argument(
        "--role",
        required=True,
        help="分工，常见的有 负责人/主讲/执行/志愿者/观众。"
             "同一个人在同一场活动里可以有多个角色，分别登记成两条",
    )
    p.add_argument("--hours", type=float, help="投入时长（小时），给轮值做参考。注意：不是五育/志愿时长")
    p.add_argument("--contribution", help="具体做了什么，一句话")
    p.add_argument("--exp-ids", help="关联的经验 id，逗号分隔，如 exp-1,exp-5")
    # ★ 这里有意没有 --honor，原因见文件开头。
    p.set_defaults(run=run_part_add)


# ══ 各动作的实现 ═════════════════════════════════════════════

def run_member_list(args, api: client.Client) -> client.Result:
    members = api.get("/api/members", keyword=args.keyword)
    return client.Result(data=members, message=f"共 {len(members)} 名成员")


def run_member_get(args, api: client.Client) -> client.Result:
    member = api.get(f"/api/members/{args.member_id}")
    return client.Result(data=member, message=member.get("name", ""))


def run_member_history(args, api: client.Client) -> client.Result:
    history = api.get(f"/api/members/{args.member_id}/history")
    return client.Result(data=history, message=f"共 {len(history)} 条履历")


def run_series_list(args, api: client.Client) -> client.Result:
    series = api.get("/api/series")
    return client.Result(data=series, message=f"共 {len(series)} 个系列")


def run_activity_list(args, api: client.Client) -> client.Result:
    activities = api.get("/api/activities", include_cancelled=args.include_cancelled)
    return client.Result(data=activities, message=f"共 {len(activities)} 场活动")


def run_part_list(args, api: client.Client) -> client.Result:
    records = api.get(
        "/api/participations",
        activity_id=args.activity_id,
        member_id=args.member_id,
    )
    return client.Result(data=records, message=f"共 {len(records)} 条参与记录")


def run_part_add(args, api: client.Client) -> client.Result:
    body = {
        "activity_id": args.activity_id,
        "member_id": args.member_id,
        "role": args.role,
    }
    if args.hours is not None:
        body["hours"] = args.hours
    if args.contribution:
        body["contribution"] = args.contribution
    if args.exp_ids:
        body["exp_ids"] = [e.strip() for e in args.exp_ids.split(",") if e.strip()]

    created = api.post("/api/participations", body)
    return client.Result(
        data=created,
        message=f"已登记：成员 {args.member_id} 在活动 {args.activity_id} 担任「{args.role}」",
    )
