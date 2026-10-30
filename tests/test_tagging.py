"""规则版打标（K8）——纯函数测试。

这个文件**不连数据库、不加载模型**，跑完不到一秒。
所以可以写得细一点：打标的坑（顺序会飘、误伤、覆盖人工标签）
全是「不报错、只是结果不对」那一类，只能靠断言钉住。

要连真库的那部分在 tests/test_tagging_run.py。
"""
import inspect

import pytest

from tagging import rules
from tagging.keywords import TAG_KEYWORDS


# ── 词表本身的规矩（keywords.py 的四条定表原则） ───────────────

def test_关键词都至少两个字():
    """★ 原则 2：中文没有词边界，单字关键词就是误伤制造机。

    「约」会命中「大约/节约/预约」，「场」会命中「现场/场地/会场」。
    写进词表的每个词都得是两个字符起步。
    """
    for name, keywords in TAG_KEYWORDS.items():
        assert keywords, f"标签「{name}」一个关键词都没有，永远打不上"
        for keyword in keywords:
            assert len(keyword) >= 2, (
                f"标签「{name}」的关键词 {keyword!r} 只有 1 个字——"
                "中文里单字到处都是，会命中一堆不相干的正文"
            )


def test_标签里不许有逗号():
    """★ 这条是从 K8 反过来捅到存储的坑。

    逗号是 Chroma 存标签的分隔符（拼成串存、读回来按逗号拆，
    见 api/vectors.py 的 _clean_metadata）。词表里写一个带逗号的标签，
    打上去、读回来会**静默变成两个**——接口不报错，标签就多了。
    所以源头就别让它进来。
    """
    for name in TAG_KEYWORDS:
        assert "," not in name and "，" not in name, f"标签「{name}」里有逗号"


def test_标签名本身非空():
    assert all(name.strip() for name in TAG_KEYWORDS)


# ── 匹配行为 ──────────────────────────────────────────────────

def test_不相干的正文一个标签都不打():
    """宁可不打，也别乱打——乱打的标签会把检索结果淹掉。"""
    assert rules.tag("") == []
    assert rules.tag("今天天气不错，去操场跑了两圈。") == []


def test_子串匹配会同时命中具体标签和泛化标签():
    """「投影仪」既是具体标签、又命中泛化的「设备」——两个都该打。

    这不是重复：按「投影仪」筛是找这台设备，按「设备」筛是找所有器材类经验。
    """
    tags = rules.tag("活动现场投影仪突然不亮，按遥控器上的 Source 键切到 HDMI2")

    assert "投影仪" in tags
    assert "设备" in tags


def test_输出顺序固定且跨调用一致():
    """★ 顺序是结果的一部分（上限截断靠它），dict 保序是唯一的依靠。

    写成 set 收标签的话，顺序会随进程的哈希种子变：
    这次 [设备, 应急预案]、下次反过来，而且本地同进程跑两遍反而一样，
    复现不出来。这条测试就是在钉这个。
    """
    content = "投影仪不亮，教室也小，还得提前申请"

    first = rules.tag(content)
    assert first == rules.tag(content)
    # 顺序 = 词表顺序（具体 → 泛化），不是命中顺序
    assert first == ["投影仪", "申请流程", "场地", "设备"]


def test_最多四个标签():
    """★ 原则 4：标签是用来筛的，一条经验挂七八个就等于没挂。

    这条正文把词表里一大半都命中了，返回的仍然只能有 4 个，
    而且留下来的必须是**词表里靠前**的（截断从后往前砍）。
    """
    content = (
        "投影仪不亮，腾讯会议要录制，纪要要转文字，备用方案是提前申请教室，"
        "线上答疑容易冷场，推文排版字号别太小，约主讲人要说清，"
        "报名接龙要统计，场地布置注意安全别腰闪了"
    )

    tags = rules.tag(content)

    assert len(tags) == rules.MAX_TAGS
    assert tags == ["投影仪", "腾讯会议", "会议记录", "应急预案"]


def test_截断留下的是具体标签而不是泛化标签():
    """★ 「具体 → 泛化」这个顺序的实际收益，用一条正文证明。

    正文同时提到设备故障和场地安全。上限 4 个一砍，
    如果泛化标签排在前面，「投影仪」这种最该命中的词就被挤出去了。
    """
    tags = rules.tag("投影仪不亮，安全第一，场地要布置，还要申请教室，线上答疑也冷场")

    assert "投影仪" in tags, "具体标签被泛化标签挤掉了——词表顺序反了"


# ── 「只补空」的结构保证 ──────────────────────────────────────

def test_tag只吃正文不吃metadata():
    """★ 结构层的「只补空」：tag() 签名里只有 content。

    它一旦能看见 metadata，就具备了「改标签」的能力，
    也就具备了覆盖人工标签的能力。现在它连看都看不到，
    所以「覆盖」这件事在这里**写不出来**，不靠谁记得别写。
    """
    assert list(inspect.signature(rules.tag).parameters) == ["content"]


def test_返回值里没有可以覆盖别人的结构():
    """tag() 返回的是「要打什么」，不是「新 metadata」。

    返回值只能是标签名的列表——调用方拿它最多只能做「加」这一个动作。
    """
    result = rules.tag("投影仪不亮")

    assert isinstance(result, list)
    assert all(isinstance(name, str) for name in result)
    assert all(name in TAG_KEYWORDS for name in result), "打出了词表里没有的标签"


# ── 打标器选择 ────────────────────────────────────────────────

def test_rules可以用():
    assert rules.ensure_available("rules") is None
    assert rules.tag_with("rules", "投影仪不亮") == ["投影仪", "设备"]


def test_llm打标器明确报未实现():
    """★ 绝不静默回退到规则版。

    回退了的话，机器人以为用的是大模型，会对结果更信任——
    错得比直接报错严重得多。
    """
    with pytest.raises(rules.TaggerNotImplemented):
        rules.tag_with("llm", "投影仪不亮")
    with pytest.raises(rules.TaggerNotImplemented):
        rules.ensure_available("llm")


def test_不认识的打标器报ValueError():
    with pytest.raises(ValueError):
        rules.ensure_available("随便写的")


def test_打标器名字里必须有rules和llm():
    """CLI 的 --tagger choices 就是这个元组，少了哪个命令行就选不到。"""
    assert set(rules.TAGGER_NAMES) == {"rules", "llm"}
