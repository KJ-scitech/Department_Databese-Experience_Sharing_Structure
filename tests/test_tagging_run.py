"""打标流程的测试：CLI 的 `exp tag` 子命令 + 它和真库的配合。

★ 假传输层是从 tests/test_cli.py 借的，不改那个文件。
  FakeTransport 有个很好用的性质：**剧本用完了还继续调用就抛错**。
  所以「第二遍一个写请求都不发」这种断言不用数请求数，
  只要剧本里不给多余的响应，多发一个请求测试就红。

★ 这个文件里的测试分两类：
  · 带 seeded 的要连服务（会加载模型，跟着大部队一起等）
  · 不带的只跑 CLI，秒级
"""
import pytest

from tagging.keywords import TAG_KEYWORDS
from tagging.rules import tag
from tests.test_cli import fake_for, run_cli


# ══ 夹具：造几条假经验 ═══════════════════════════════════════

def _exp(exp_id: str, content: str, tags=None) -> dict:
    """造一条经验，形状和服务端 GET /api/experience 返回的一致。"""
    metadata = {"type": "笔记", "created_at": "2026-09-20 10:00:00"}
    if tags:
        metadata["tags"] = tags
    return {"exp_id": exp_id, "content": content, "metadata": metadata}


def _patched(exp_id: str, tags: list[str]) -> dict:
    """一条 PATCH 成功的响应（服务端 PATCH 返回整条经验）。"""
    return {"exp_id": exp_id, "content": "（正文无关紧要）", "metadata": {"tags": tags}}


def _methods(fake) -> list[str]:
    return [call["method"] for call in fake.calls]


# ══ 只补空 ═══════════════════════════════════════════════════

def test_只给没标签的补(capsys):
    fake = fake_for(
        (200, [
            _exp("exp-1", "投影仪不亮，按遥控器切信号源"),          # 没标签 → 补
            _exp("exp-2", "推文排版字号别小于 14px", ["推文", "排版"]),  # 有标签 → 跳过
        ]),
        (200, _patched("exp-1", ["投影仪", "设备"])),
    )

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "2"], capsys)

    assert code == 0
    assert envelope["data"]["scanned"] == 2
    assert envelope["data"]["tagged"] == 1
    assert envelope["data"]["already_tagged"] == 1
    assert [c["url"].rsplit("/", 1)[-1] for c in fake.calls if c["method"] == "PATCH"] == ["exp-1"]
    assert "补了 1 条" in envelope["message"]


def test_人工标签一个字都不动(capsys):
    """★★ 「只补空」的验收：已经有标签的那条，**一个写请求都不该发**。

    剧本里只留了一个 PATCH 响应，却有两百条已带标签的经验——
    CLI 只要敢给 exp-2 发一个写请求，FakeTransport 就会因为剧本用完而抛错。
    """
    page = [_exp(f"exp-{i}", "投影仪不亮", ["人工写的标签"]) for i in range(1, 201)]
    fake = fake_for((200, page))

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "200"], capsys)

    assert code == 0
    assert envelope["data"]["tagged"] == 0
    assert "PATCH" not in _methods(fake)


# ══ 幂等 ═════════════════════════════════════════════════════

def test_第二遍一条都不补也不发写请求(capsys):
    """★ K8 幂等：补过之后再跑一遍，扫描数不变、补 0 条、写请求 0 个。

    这条是「敢在服务器上挂着重复跑」的前提。
    """
    # 第一遍跑完的库：两条都有标签了
    fake = fake_for(
        (200, [
            _exp("exp-1", "投影仪不亮", ["投影仪", "设备"]),
            _exp("exp-2", "教室要提前申请", ["场地", "申请流程"]),
        ]),
    )

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "2"], capsys)

    assert code == 0
    assert envelope["data"]["scanned"] == 2, "第二遍就不该再扫库了？不，还是要扫，只是不写"
    assert envelope["data"]["tagged"] == 0
    assert envelope["data"]["already_tagged"] == 2
    assert _methods(fake) == ["GET"], "第二遍发了写请求，幂等破了"


def test_两次打标给出的标签完全一样():
    """★ 幂等的另一半：tag() 是正文的纯函数，输出顺序固定。

    如果它哪天开始返回不稳定的顺序，第一遍和第二遍打出来的标签会不一样，
    「重跑一遍」就不再安全了。（顺序会被写进存储，因为 PATCH 是整份替换。）
    """
    content = "投影仪不亮，教室要申请，线上答疑容易冷场"

    assert tag(content) == tag(content)


# ══ 写请求的形状 ═════════════════════════════════════════════

def test_写请求只带tags字段(capsys):
    """★★ PATCH 的请求体只有 tags。

    正文、作者、活动 id 都不在这次的职责里，服务端也只收 tags
    （多传字段会 422）。tags 是整份替换，所以发的是完整的标签列表。
    """
    fake = fake_for(
        (200, [_exp("exp-1", "投影仪不亮")]),
        (200, _patched("exp-1", ["投影仪", "设备"])),
    )

    run_cli(fake, ["exp", "tag", "--limit", "1"], capsys)

    body = next(c["json"] for c in fake.calls if c["method"] == "PATCH")
    assert set(body) == {"tags"}
    assert body["tags"] == ["投影仪", "设备"]


def test_打的是规则版算出来的标签(capsys):
    fake = fake_for(
        (200, [_exp("exp-1", "腾讯会议记得开录制，会后转文字整理纪要")]),
        (200, _patched("exp-1", [])),
    )

    run_cli(fake, ["exp", "tag", "--limit", "1"], capsys)

    body = next(c["json"] for c in fake.calls if c["method"] == "PATCH")
    assert body["tags"] == ["腾讯会议", "会议记录"]


# ══ dry-run ══════════════════════════════════════════════════

def test_dry_run一个写请求都不发(capsys):
    """第一次跑生产库先加 --dry-run：看清楚了再动。"""
    fake = fake_for((200, [_exp("exp-1", "投影仪不亮"), _exp("exp-2", "教室要申请")]))

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "2", "--dry-run"], capsys)

    assert code == 0
    assert envelope["data"]["dry_run"] is True
    assert envelope["data"]["tagged"] == 0
    assert len(envelope["data"]["pending"]) == 2, "dry-run 也要把打算打什么列出来"
    assert _methods(fake) == ["GET"]


# ══ 命中不了 / 打标器未实现 ══════════════════════════════════

def test_没命中关键词的不算补了(capsys):
    """一个关键词都没命中就留着，不硬塞标签——乱打的标签会淹掉检索。"""
    fake = fake_for((200, [_exp("exp-1", "今天天气不错，去操场跑了两圈")]))

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "1"], capsys)

    assert code == 0
    assert envelope["data"]["tagged"] == 0
    assert envelope["data"]["no_keyword_match"] == 1
    assert _methods(fake) == ["GET"]


def test_llm打标器退出码是2且一个请求都不发(capsys):
    """★ 不静默回退到规则版，而且要在扫库**之前**就失败。

    剧本是空的：CLI 只要发出任何一个请求，FakeTransport 就会抛错——
    所以「退出码 2」和「没发请求」这两件事一次断言完。
    """
    fake = fake_for()

    code, envelope = run_cli(fake, ["exp", "tag", "--tagger", "llm"], capsys)

    assert code == 2, "参数问题应该是 2（改命令），不是 1（查服务）"
    assert envelope["ok"] is False
    assert envelope["code"] == "not_implemented"
    assert fake.calls == []


# ══ 翻页 ═════════════════════════════════════════════════════

def test_超过一页会翻页(capsys):
    """服务端一页最多 200 条（Query(50, ge=1, le=200)）。

    --limit 250 就应该：先要 200，再要 50。
    """
    page_one = [_exp(f"exp-{i}", "今天天气不错") for i in range(1, 201)]
    page_two = [_exp(f"exp-{i}", "今天天气不错") for i in range(201, 211)]
    fake = fake_for((200, page_one), (200, page_two))

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "250"], capsys)

    assert code == 0
    gets = [c for c in fake.calls if c["method"] == "GET"]
    assert [c["params"] for c in gets] == [
        {"limit": 200, "offset": 0},
        {"limit": 50, "offset": 200},  # 第二页只剩 50 条的额度
    ]
    assert envelope["data"]["scanned"] == 210


def test_提前到底就停(capsys):
    """页面比要的少，说明到底了，不该再发一个请求去确认。"""
    fake = fake_for((200, [_exp(f"exp-{i}", "今天天气不错") for i in range(1, 4)]))

    code, envelope = run_cli(fake, ["exp", "tag", "--limit", "200"], capsys)

    assert code == 0
    assert envelope["data"]["scanned"] == 3
    assert _methods(fake) == ["GET"]


# ══ 和真库的配合（要服务） ═══════════════════════════════════

def test_种子语料全都已经有人工标签(seeded):
    """★ 这条是「种子上跑 exp tag 应该补 0 条」的前提，钉住它。

    种子一跑完，8 条经验全带人工标签，所以：
        python -m cli exp tag --dry-run --pretty   →  「会补 0 条」
    这不是打标器坏了，是本来就没什么可补的。
    """
    items = seeded.get("/api/experience", params={"limit": 200}).json()

    assert items, "库是空的，先跑 python -m scripts.seed"
    without = [i["exp_id"] for i in items if not i["metadata"]["tags"]]
    assert not without, f"这些经验没有标签，K8 跑第一遍就不再是 0 条了：{without}"


def test_词表覆盖语料里所有在用的标签(seeded):
    """★ keywords.py 原则 1「标签是闭集」的守门测试。

    语料里冒出一个词表里没有的标签 → 规则版永远打不出它，
    按这个标签筛的时候就会漏，而且是静默地漏。

    红了怎么办：要么把新标签加进 taggging/keywords.py（连带它的关键词），
    要么说明那个人工标签写错了。
    """
    items = seeded.get("/api/experience", params={"limit": 200}).json()
    in_use = {name for item in items for name in item["metadata"]["tags"]}

    missing = sorted(in_use - set(TAG_KEYWORDS))
    assert not missing, (
        f"这些标签语料里在用，但 tagging/keywords.py 的词表里没有：{missing}。"
        "请把它们（及其关键词）加进词表，或者修正那条人工标签"
    )


def test_规则打标能覆盖种子的人工标签(seeded):
    """★ 校准数字的测试化：种子 8 条的人工标签，规则版要能全部打出来。

    实测 17/17 个标签、8/8 条。规则版还会**多打**几个（实测 4 个），
    所以这里断言的是「人工标签 ⊆ 规则标签」，不是相等。

    只看 exp-1..exp-8：这 8 条是词表的「训练集」（词表就是照它们写的），
    后来的真实数据不归这条测试管——换了语料要重新量，不是重新套。
    """
    for index in range(1, 9):
        resp = seeded.get(f"/api/experience/exp-{index}")
        assert resp.status_code == 200, f"exp-{index} 不在了，种子数据变过？"

        item = resp.json()
        human = set(item["metadata"]["tags"])
        produced = set(tag(item["content"]))

        missed = sorted(human - produced)
        assert not missed, f"exp-{index} 的人工标签 {missed} 规则版打不出来，词表要补关键词"
