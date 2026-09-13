# -*- coding: utf-8 -*-
"""统计报表的 CLI 子命令：by-member / never-participated / rotation。

对应服务端的 api/routers/reports.py（K5）。

这三张表回答的是同一类问题，但角度不同：

  by-member          每个人干了几次、各是什么角色
  never-participated 谁一次都还没干过
  rotation           按学期看，活是不是压在少数人身上

═══════════════════════════════════════════════════════════════════
  报表里**没有**五育 / 志愿时长
═══════════════════════════════════════════════════════════════════

需求文档写明了：五育时长、志愿时长不由本系统产出。
这里只统计「谁干了几次、干了什么角色」，是安排轮值和换届交接时的参考，
不是发放时长的依据。

part add 里那个 --hours 也一样，它只是给轮值当参考的一个数，
不要拿它当时长报上去。

═══════════════════════════════════════════════════════════════════
  by-member 默认不列已退部的人
═══════════════════════════════════════════════════════════════════

默认只看 status = "在部" 的人。要连已退部的一起看，加 --include-inactive。

★ 但「一次都没参与过」的人是**一定**会出现的，次数显示 0。
  这不是 bug，是故意的：服务端用的是 LEFT JOIN 而不是 INNER JOIN，
  就是为了让这些人留在结果里——「谁还没干过」恰恰是轮值最需要的信息。
  用 INNER JOIN 的话这些人会从报表里整批消失，而且看不出少了人。
"""
from . import client


# ══ 子命令注册 ═══════════════════════════════════════════════

def register(sub, parents) -> None:
    parser = sub.add_parser("report", help="统计报表：工作量、谁没干过、轮值分布")
    actions = parser.add_subparsers(dest="action", metavar="动作", required=True)

    p = actions.add_parser(
        "by-member",
        parents=parents,
        help="按人聚合：每个人参与了几次、各是什么角色、拿过几次荣誉",
    )
    p.add_argument(
        "--include-inactive",
        action="store_true",
        help="把已退部的人也列出来（默认只看「在部」的）",
    )
    p.set_defaults(run=run_by_member)

    p = actions.add_parser(
        "never-participated",
        parents=parents,
        help="谁一次活动都没参与过——安排轮值时先看这个",
    )
    p.set_defaults(run=run_never_participated)

    p = actions.add_parser(
        "rotation",
        parents=parents,
        help="按学期看：办了几场、多少人参与、人均几场",
    )
    p.set_defaults(run=run_rotation)


# ══ 各动作的实现 ═════════════════════════════════════════════

def run_by_member(args, api: client.Client) -> client.Result:
    rows = api.get("/api/reports/by-member", only_active=not args.include_inactive)
    # 顺手把「一次都没干过」的人数出来——这是报表里最该被看见的一个数，
    # 但要人去 8 行结果里数 0 太费劲了。
    idle = [r for r in rows if r.get("total", 0) == 0]
    message = f"共 {len(rows)} 人，其中 {len(idle)} 人还没参与过活动"
    return client.Result(data=rows, message=message)


def run_never_participated(args, api: client.Client) -> client.Result:
    members = api.get("/api/reports/never-participated")
    if not members:
        # 全员都干过活，这是好消息，不是错误。
        return client.Result(data=[], message="在部成员都参与过活动")
    return client.Result(data=members, message=f"{len(members)} 人还没参与过活动")


def run_rotation(args, api: client.Client) -> client.Result:
    rows = api.get("/api/reports/rotation")
    return client.Result(data=rows, message=f"共 {len(rows)} 个学期")
