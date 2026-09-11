# -*- coding: utf-8 -*-
"""CLI 的验收测试。

═══════════════════════════════════════════════════════════════════
  为什么这个文件跟别的测试长得不一样
═══════════════════════════════════════════════════════════════════

别的测试文件是 `client.get(...)` 打进去、看服务端返回什么。
这个文件**完全不碰服务端**——它测的是 CLI 自己那一层。

理由是「退出码」这个东西只有真的走一遍 main() 才测得到。
而 main() 的真实路径要走 HTTP，那就要起服务、加载模型（20–30 秒）。
为了验证「404 是不是返回 4」等半分钟，没人会愿意跑。

做法：给 Client 塞一个**假的传输层**（transport），
它不联网，直接按剧本返回 (状态码, 响应正文)，或者按剧本抛连接错误。
CLI 的错误映射、信封结构、退出码全都能钉死，而且跑一遍不到一秒。

这一层是最该被测的：机器人的分支逻辑全压在退出码上，
退出码悄悄改了但没人发现，机器人就会把「已经登记过了」当失败重试。

★ 这里没测的东西（也别在这测）：
  · 具体业务逻辑对不对 —— 那是 test_experience.py / test_directory.py 的事
  · 真实网络的超时行为 —— 假的传输层模拟不出来
  · argparse 的用法错误 —— 它自己就退 2，没什么好测的
"""
import json

import pytest
import requests

from cli import client as cli_client
from cli.main import main


# ══ 假传输层 ═════════════════════════════════════════════════

class FakeTransport:
    """按剧本返回响应，不联网。

    剧本是一串 (状态码, 正文) ；正文给 dict 就自动转 JSON 字符串。
    剧本用完了还继续调用会抛错——说明 CLI 多发了请求，那是 bug。
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, params, json_body, timeout):
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body})
        if not self.responses:
            raise AssertionError(f"剧本里没有更多响应了，但 CLI 又发了第 {len(self.calls)} 个请求")
        status, body = self.responses.pop(0)
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        return status, body


def _invoke(fake, argv) -> int:
    """跑一遍 main()，把假传输层装进去，返回退出码。**不读 stdout。**

    main() 里是自己 new 的 Client，没法从外面注入，所以这里临时把
    cli.main.client.Client 换成一个「构造时自动带上假传输层」的子类。
    用完还原——注意要还原 cli.main 里那个名字而不是 cli.client 里的，
    因为 main.py 是 `from . import client` 拿到模块再取属性的。
    """
    class Patched(cli_client.Client):
        def __init__(self, base_url=None, transport=None, timeout=cli_client.TIMEOUT):
            super().__init__(base_url=base_url, transport=fake, timeout=timeout)

    import cli.main as cli_main

    original = cli_main.client.Client
    cli_main.client.Client = Patched
    try:
        return main(argv)
    finally:
        cli_main.client.Client = original


def run_cli(fake, argv, capsys):
    """跑一遍，返回 (退出码, 解析后的信封)。

    ★ capsys.readouterr() 会把缓冲区清空，所以一个测试里只能读一次。
      既要信封又要原始文本的测试，用下面的 run_cli_text。
    """
    code = _invoke(fake, argv)
    captured = capsys.readouterr().out.strip()
    return code, json.loads(captured.splitlines()[-1])


def run_cli_text(fake, argv, capsys):
    """跑一遍，返回 (退出码, stdout 原文)。用于检查「输出长什么样」。"""
    code = _invoke(fake, argv)
    return code, capsys.readouterr().out


def fake_for(*responses):
    return FakeTransport(*responses)


# ══ 信封的形状 ═══════════════════════════════════════════════

ENVELOPE_KEYS = {"ok", "code", "data", "message", "low_confidence"}


def test_信封永远是这五个键(capsys):
    """机器人是照这五个键写的。少一个它就得写防御代码，多一个它可能就不认了。"""
    fake = fake_for((200, []))
    _, envelope = run_cli(fake, ["exp", "list"], capsys)
    assert set(envelope) == ENVELOPE_KEYS


def test_成功时退出码是0且ok为真(capsys):
    fake = fake_for((200, [{"exp_id": "exp-1"}]))
    code, envelope = run_cli(fake, ["exp", "list"], capsys)
    assert code == cli_client.EXIT_OK
    assert envelope["ok"] is True
    assert envelope["code"] == "ok"


def test_stdout里只有一行JSON(capsys):
    """多余的输出会污染机器人的解析。消息也要进信封，不能另起一行 print。"""
    fake = fake_for((200, []))
    _, out = run_cli_text(fake, ["exp", "list"], capsys)
    assert len(out.strip().splitlines()) == 1
    json.loads(out)


# ══ 错误 → 退出码的映射（这张表就是机器人的分支逻辑）═══

def test_404映射成退出码4(capsys):
    fake = fake_for((404, {"detail": "经验 exp-999 不存在"}))
    code, envelope = run_cli(fake, ["exp", "get", "exp-999"], capsys)
    assert code == cli_client.EXIT_NOT_FOUND
    assert envelope["code"] == "not_found"
    assert envelope["ok"] is False


def test_409映射成退出码5但算成功(capsys):
    """★ 这条最容易被改坏。

    409 是「你要做的事已经做过了」——机器人重试同一条登记时会走到这里。
    如果把它当失败，机器人会告诉群里「登记失败了」，其实人家早就登记好了。
    所以退出码是 5（好让机器人能分辨），但 ok 必须是 true。
    """
    fake = fake_for((409, {"detail": "成员 2 在活动 1 已经是「主讲」了，不用重复登记"}))
    code, envelope = run_cli(
        fake,
        ["part", "add", "--activity-id", "1", "--member-id", "2", "--role", "主讲"],
        capsys,
    )
    assert code == cli_client.EXIT_CONFLICT
    assert envelope["ok"] is True
    assert envelope["code"] == "conflict"


def test_400映射成退出码6(capsys):
    fake = fake_for((400, {"detail": "author_id=99 不存在"}))
    code, envelope = run_cli(fake, ["exp", "get", "exp-1"], capsys)
    assert code == cli_client.EXIT_REJECTED
    assert envelope["code"] == "rejected"


def test_422的detail是列表也能读(capsys):
    """★ 两种错误体形状都要认。

    HTTPException 抛出来的是 {"detail": "一句话"}；
    Pydantic 校验失败返回的 422 是 {"detail": [ {...}, ... ]}，是个**列表**。
    直接 str() 出来会是一坨 Python 字典，机器人念给用户听就是灾难。
    """
    fake = fake_for(
        (
            422,
            {
                "detail": [
                    {
                        "type": "string_too_short",
                        "loc": ["body", "content"],
                        "msg": "String should have at least 1 character",
                        "input": "",
                    }
                ]
            },
        )
    )
    code, envelope = run_cli(
        fake,
        ["exp", "add", "--content", "x"],
        capsys,
    )
    assert code == cli_client.EXIT_REJECTED
    # 关键：message 是人话，不是 "{'type': 'string_too_short', 'loc': ...}"
    assert "content" in envelope["message"]
    assert "at least 1 character" in envelope["message"]
    assert "{" not in envelope["message"]


def test_连不上映射成退出码3(capsys):
    def boom(method, url, params, json_body, timeout):
        raise requests.ConnectionError("Connection refused")

    import cli.main as cli_main

    class Patched(cli_client.Client):
        def __init__(self, base_url=None, transport=None, timeout=cli_client.TIMEOUT):
            super().__init__(base_url=base_url, transport=boom, timeout=timeout)

    original = cli_main.client.Client
    cli_main.client.Client = Patched
    try:
        code = main(["health"])
    finally:
        cli_main.client.Client = original

    envelope = json.loads(capsys.readouterr().out.strip())
    assert code == cli_client.EXIT_UNREACHABLE
    assert envelope["code"] == "unreachable"


def test_返回的不是JSON映射成退出码1(capsys):
    """打到了别的服务上、或者被反代塞了一页 HTML 错误页。

    这种时候任何「成功」的判断都是假的，必须报错而不是当空结果。
    """
    fake = fake_for((200, "<html><body>502 Bad Gateway</body></html>"))
    code, envelope = run_cli(fake, ["exp", "list"], capsys)
    assert code == cli_client.EXIT_ERROR
    assert envelope["code"] == "bad_response"


# ══ health 的降级状态 ════════════════════════════════════════

def test_数据库挂了是降级不是断线(capsys):
    """★ /api/health 在 MySQL 挂掉时仍然返回 **HTTP 200**，只是 ok:false。

    所以不能用「抛没抛异常」来判断。这是「服务在，但降级了」（7），
    和「服务没了」（3）要说的话不一样，机器人得能分开。
    """
    fake = fake_for(
        (
            200,
            {
                "ok": False,
                "database": "连接失败：OperationalError（scitech_kb）",
                "experience_count": 0,
                "model": "BAAI/bge-small-zh-v1.5",
                "embedding_dim": 512,
            },
        )
    )
    code, envelope = run_cli(fake, ["health"], capsys)
    assert code == cli_client.EXIT_DEGRADED
    assert envelope["code"] == "degraded"
    assert envelope["ok"] is False
    # 详情要留着——运维要靠它才知道是数据库的问题
    assert "OperationalError" in envelope["data"]["database"]


def test_健康时退出码0(capsys):
    fake = fake_for(
        (
            200,
            {
                "ok": True,
                "database": "已连接（scitech_kb）",
                "experience_count": 8,
                "model": "BAAI/bge-small-zh-v1.5",
                "embedding_dim": 512,
            },
        )
    )
    code, envelope = run_cli(fake, ["health"], capsys)
    assert code == cli_client.EXIT_OK
    assert envelope["ok"] is True


# ══ exp search 的相似度下限 ═════════════════════════════════

def _hit(exp_id, similarity):
    return {"exp_id": exp_id, "content": f"内容 {exp_id}", "similarity": similarity, "metadata": {}}


def test_搜到相关的就正常返回(capsys):
    """最像的一条够高（≥0.55），就正常返回，不置 low_confidence。"""
    fake = fake_for((200, [_hit("exp-1", 0.578), _hit("exp-2", 0.51), _hit("exp-3", 0.12)]))
    code, envelope = run_cli(fake, ["exp", "search", "设备出故障了咋办"], capsys)
    assert code == cli_client.EXIT_OK
    assert envelope["code"] == "ok"
    # 0.12 被 0.45 的下限挡在外面
    assert [h["exp_id"] for h in envelope["data"]] == ["exp-1", "exp-2"]
    assert envelope["low_confidence"] is False


def test_一条都不够像时是no_match不是错误(capsys):
    """★ 这条是给机器人兜底的。

    服务端的检索**没有相似度下限**，库里只要有数据，问什么都返回 top_k 条。
    群里有人问「今天中午吃啥」，会拿到一堆 0.2 上下的经验。
    机器人要是把第一条当答案发出去，比说「我不知道」糟糕得多。

    所以 CLI 自己筛。全被筛掉时：
      · 退出码仍然是 0 —— 「没找到」是正常答案，不是错误
      · code 变成 no_match —— 机器人靠这个决定措辞
      · data 是空列表，不是那些凑数的结果
      · low_confidence 置 true
    """
    fake = fake_for((200, [_hit("exp-3", 0.21), _hit("exp-5", 0.18), _hit("exp-1", 0.09)]))
    code, envelope = run_cli(fake, ["exp", "search", "今天中午吃啥"], capsys)

    assert code == cli_client.EXIT_OK
    assert envelope["ok"] is True
    assert envelope["code"] == "no_match"
    assert envelope["data"] == []
    assert envelope["low_confidence"] is True
    # 最像的那条分数要报出来，方便定位是阈值定太高还是真的没有
    assert "0.21" in envelope["message"]


def test_把握不大时照常返回但标记出来(capsys):
    """★ 第二档：过了下限、但不够有把握。

    实测里「相关」和「不相关」的分数区间**是重叠的**
    （不相干最高 0.5373，相关最低 0.4920），没有哪个数字能把两者切开。
    所以这一档不丢弃，而是照常返回 + 把 low_confidence 置起来，
    让机器人说「可能相关」而不是「就是这条」。

    宁可放过、不可错杀：库里有答案却告诉用户「没找到」，
    比给一条不太准的、用户自己看得出不靠谱，更糟。
    """
    fake = fake_for((200, [_hit("exp-2", 0.4920), _hit("exp-9", 0.20)]))
    code, envelope = run_cli(fake, ["exp", "search", "场地申请流程"], capsys)

    assert code == cli_client.EXIT_OK
    assert envelope["code"] == "ok"
    # 结果照给——0.4920 那条是真答案，不能因为把握不大就吞掉
    assert [h["exp_id"] for h in envelope["data"]] == ["exp-2"]
    # 但必须标记出来，机器人据此把话说软
    assert envelope["low_confidence"] is True
    assert "把握不大" in envelope["message"]


def test_经验库是空的时候也说no_match(capsys):
    fake = fake_for((200, []))
    code, envelope = run_cli(fake, ["exp", "search", "随便问问"], capsys)
    assert code == cli_client.EXIT_OK
    assert envelope["code"] == "no_match"


def test_阈值可以用参数调(capsys):
    """网页那边要看全部原始结果，所以下限必须能关掉。"""
    fake = fake_for((200, [_hit("exp-1", 0.31), _hit("exp-2", 0.22)]))
    code, envelope = run_cli(
        fake, ["exp", "search", "什么问题都行", "--min-similarity", "0"], capsys
    )
    assert code == cli_client.EXIT_OK
    assert envelope["code"] == "ok"
    assert len(envelope["data"]) == 2
    # 关了下限不等于「这些就靠谱了」——把握不大这件事是结果的属性，照旧标记
    assert envelope["low_confidence"] is True


# ══ 请求拼得对不对 ═══════════════════════════════════════════

def test_检索请求带了该带的参数(capsys):
    fake = fake_for((200, []))
    run_cli(fake, ["exp", "search", "投影仪不亮", "--top-k", "3"], capsys)
    sent = fake.calls[0]
    assert sent["method"] == "GET"
    assert sent["url"].endswith("/api/experience/search")
    assert sent["params"]["q"] == "投影仪不亮"
    assert sent["params"]["top_k"] == 3


def test_没传的筛选项不发出去(capsys):
    """--keyword 没给时不能发 keyword=None 或 keyword=""，
    那会让服务端按空串去 like 一遍，白跑一趟。"""
    fake = fake_for((200, []))
    run_cli(fake, ["member", "list"], capsys)
    assert fake.calls[0]["params"]["keyword"] is None


def test_登记分工不带荣誉字段(capsys):
    """★ 这条测的是一个**故意的缺失**。

    /api/reports/by-member 的荣誉次数是评优依据，而写接口没有鉴权。
    机器人要是能填 honor，群里任何人都能给自己报荣誉。
    所以 CLI 根本不暴露这个参数——测一下它确实没被带上。

    （命令行上就算硬写 --honor，argparse 也会以退出码 2 拒绝。）
    """
    fake = fake_for((201, {"ok": True, "id": 9}))
    run_cli(
        fake,
        ["part", "add", "--activity-id", "1", "--member-id", "2", "--role", "执行"],
        capsys,
    )
    body = fake.calls[0]["json"]
    assert "honor" not in body
    assert body == {"activity_id": 1, "member_id": 2, "role": "执行"}


def test_参数写错时退出码是2(capsys):
    """--honor 是个不存在的参数，argparse 自己就会拒掉。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["part", "add", "--activity-id", "1", "--member-id", "2", "--role", "执行", "--honor", "优秀"])
    assert excinfo.value.code == cli_client.EXIT_USAGE


def test_pretty写在子命令后面也生效(capsys):
    """argparse 的经典坑：--pretty 定义在顶层的话，写在子命令后面就不认了。

    这里靠 parents + SUPPRESS 让它两种位置都行。
    SUPPRESS 是关键——不加的话子解析器会用默认值把顶层解析好的值覆盖掉。
    """
    fake = fake_for((200, [{"id": 1}]))
    code, out = run_cli_text(fake, ["exp", "list", "--pretty"], capsys)
    assert code == cli_client.EXIT_OK
    # 缩进过的话就有多个换行；单行 JSON 只有一个
    assert len(out.strip().splitlines()) > 1
