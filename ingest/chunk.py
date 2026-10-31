# -*- coding: utf-8 -*-
"""长文切片：按句子边界攒到目标长度，相邻切片重叠若干**完整句子**。

★ 这个模块只依赖标准库。`tests/test_ingest.py` 要在只装了
  requirements.txt 的 CI 上跑，而切片规则本身也是最值得单测的地方。

★ 重叠意味着 `"".join(split_text(t).chunks) != t`。这是**有意的**：
  相邻两片共享约 50 字的完整句子，检索时答案落在边界上也不会被切掉一半。
  不要把它当 bug「修」掉。

★ 输出必须确定：同样的输入 + 同样的参数，每次都产出同样的切片。
  入库前的重复探测是按 `content` 全等判断的（见 ingest/store.py），
  切片一变，去重就对不上了。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 句子边界：中文句号/问号/叹号/分号 + 英文对应 + 换行。
# 用零宽前瞻的 split 写法，标点留在句子末尾。
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;\n])")

# 长句硬切时优先往回找的「软停顿」符号。
_SOFT_BREAK = set("，、,;；:：")


@dataclass
class SplitResult:
    """切片结果 + 过程统计。

    统计不是装饰：硬断是唯一会切坏句子的路径，必须让调用方看得见，
    否则「精度不行」会被误以为是模型的问题。
    """

    chunks: list[str] = field(default_factory=list)
    hard_cuts: int = 0     # 超长句找不到软停顿、只能硬断的次数
    soft_cuts: int = 0     # 超长句在逗号/顿号处断开的次数
    merged_tail: int = 0   # 尾部碎片并进上一片（或与上一片重叠抵消）的次数


_WS = " \t\r\f\v"   # 空白，但**不含** \n


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，标点保留在句尾，空句丢掉。

    ★ 换行也保留在句尾。表格提取出来的是「一行一条」，如果这里把 \n
      一并 strip 掉，攒片时上一行的最后一格会和下一行的第一格粘成
      「…在抽屉话筒 | …」——行界没了。它不会报错，只会让入库的内容
      悄悄变难读。
    """
    sentences = []
    for part in _SENTENCE_END.split(text):
        part = part.strip(_WS)
        if part.strip():      # 只用换行/空白组成的片段仍算空句
            sentences.append(part)
    return sentences


def _hard_split(sentence: str, size: int, search_back: int = 100):
    """把超长句拆成 <= size 的片段。

    优先在 size 往前 search_back 字内的逗号/顿号处断——
    PDF 里整段没有句号的文字经常这样，退一步在逗号处断，
    比在字中间硬断好。实在找不到才硬断。

    返回 (片段列表, 硬断次数, 软断次数)。
    """
    pieces: list[str] = []
    hard = soft = 0
    rest = sentence

    while len(rest) > size:
        cut = -1
        lower = max(0, size - search_back)
        for i in range(size, lower, -1):
            if rest[i - 1] in _SOFT_BREAK:
                cut = i
                break
        if cut > 0:
            soft += 1
        else:
            cut = size
            hard += 1
        pieces.append(rest[:cut])
        rest = rest[cut:]

    if rest:
        pieces.append(rest)
    return pieces, hard, soft


def _overlap_tail(prev_chunk: str, prev_sentences: list[str], budget: int):
    """从上一片末尾往回攒**完整句子**，作为下一片的开头。

    返回 (重叠文本, 重叠里包含的句子列表)。攒不出完整句子
    （上一片本身就是一句超长句）就返回空——宁可少重叠，
    也不要半句话：半句话的向量是噪声。
    """
    if budget <= 0:
        return "", []

    acc = ""
    acc_sentences: list[str] = []
    for sentence in reversed(prev_sentences):
        if acc and len(acc) + len(sentence) > budget:
            break
        acc = sentence + acc
        acc_sentences.insert(0, sentence)
        if len(acc) >= budget:
            break

    # 整片都当成重叠没有意义——下一片会变成上一片的复制。
    if not acc or acc == prev_chunk:
        return "", []
    return acc, acc_sentences


def split_text(
    text: str,
    size: int = 400,
    overlap: int = 50,
    min_chunk: int = 15,
) -> SplitResult:
    """长文 → 切片。

    规则（缺一条都会掉信息）：
      1. 先按句切，再贪心攒到 size —— 不切在句子中间。
      2. 重叠按整句取（见 _overlap_tail），不按字符数取。
      3. 单句超长时在 size 处找软停顿断开，统计硬断次数。
      4. 尾部碎片并进上一片（允许上一片略微超过 size），而不是丢掉。

    ★ `min_chunk` 只在**最后一片**上生效。中间的片如果短于它，
      说明下一句太长了——这个情况在下面的循环里就已经避免掉了。
    """
    if size <= 0:
        raise ValueError("size 必须大于 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须 >= 0 且小于 size")
    if min_chunk < 0:
        raise ValueError("min_chunk 不能为负")

    result = SplitResult()

    # 先把超长句拆成能参与贪心攒片的单位。
    prepared: list[str] = []
    for sentence in split_sentences(text):
        if len(sentence) <= size:
            prepared.append(sentence)
            continue
        pieces, hard, soft = _hard_split(sentence, size)
        result.hard_cuts += hard
        result.soft_cuts += soft
        prepared.extend(pieces)

    if not prepared:
        return result

    chunks: list[str] = []
    # 每片开头用了多少字的「重叠前缀」。尾片并进上一片时要去掉这一段，
    # 否则重叠的部分会在上一片里出现两次。
    prefix_lengths: list[int] = []

    current = ""
    current_sentences: list[str] = []
    current_prefix_len = 0

    for sentence in prepared:
        too_long = current and len(current) + len(sentence) > size
        # ★ 当前片还不足 min_chunk 时不急着 flush：让下一句并进来，
        #   避免产出一个「好。」这样的中间碎片。硬拆后的句子 <= size，
        #   所以这里最多超出 size + min_chunk，不会失控。
        if too_long and len(current) >= min_chunk:
            chunks.append(current.strip())
            prefix_lengths.append(current_prefix_len)

            tail, tail_sentences = _overlap_tail(
                current.strip(), current_sentences, overlap
            )
            current = tail + sentence
            current_sentences = tail_sentences + [sentence]
            current_prefix_len = len(tail)
        else:
            current += sentence
            current_sentences.append(sentence)

    if current.strip():
        chunks.append(current.strip())
        prefix_lengths.append(current_prefix_len)

    # ── 尾部碎片并进上一片 ────────────────────────────────────
    #
    # 丢数据比多几十个字更糟。如果尾片整段都是重叠（prefix 覆盖了全文），
    # 那它本来就没有新信息，直接丢掉不算丢数据。
    if len(chunks) >= 2 and len(chunks[-1]) < min_chunk:
        tail = chunks.pop()
        prefix_len = prefix_lengths.pop()
        body = tail[prefix_len:]
        if body:
            # pop 之后，上一片就是新的最后一片（不是 chunks[-2]）。
            chunks[-1] += body
        result.merged_tail += 1

    result.chunks = [c for c in chunks if c]
    return result
