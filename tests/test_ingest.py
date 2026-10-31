# -*- coding: utf-8 -*-
"""K7 长文进库的单元测试。

★ 这个文件**不 import faster_whisper / openpyxl / pdfplumber**。
  切片、编码回退、单元格格式化、载荷构造都是纯逻辑，没有理由为它们
  等模型或二进制包。`ingest/extract.py` 的惰性导入正好保证这一点。

★ 重点是三类「错了也不会报错，只会静默出错」的地方：
    1. 切片重叠必须落在整句边界（半句话的向量是噪声）；
    2. GBK 文件不能被当成 UTF-8 解成乱码；
    3. 入库去重必须 content 与 source 同时相等（K1 没有幂等键）。
"""
import datetime

from ingest import chunk, extract, store, transcribe


# ══ 切片 ══════════════════════════════════════════════════════

def _common_overlap(previous: str, following: str) -> str:
    """求「上一片的真后缀 == 下一片的假前缀」的最长串。"""
    for size in range(min(len(previous), len(following)), 0, -1):
        if previous.endswith(following[:size]):
            return following[:size]
    return ""


class TestSplitText:
    def test_empty_text_has_no_chunks(self):
        assert chunk.split_text("").chunks == []
        assert chunk.split_text("  \n  ").chunks == []

    def test_short_text_stays_one_chunk(self):
        text = "今天讲一下设备巡检。上周三三号机温度偏高。"
        assert chunk.split_text(text).chunks == [text]

    def test_row_newlines_survive_splitting(self):
        """表格的「一行一条」不能因为切片粘成一坨（见 split_sentences）。"""
        text = "设备 | 编号\n投影仪 | 20230101\n话筒 | 2015"
        assert chunk.split_text(text).chunks == [text]

    def test_no_overlap_is_lossless(self):
        text = "".join(f"第{i}句讲的是第{i}个注意事项。" for i in range(1, 100))
        result = chunk.split_text(text, size=120, overlap=0)
        assert "".join(result.chunks) == text

    def test_overlap_is_a_run_of_whole_sentences(self):
        """重叠必须从整句开头、到整句结尾——不能是半句话。"""
        sentences = [f"第{i}句讲的是第{i}个注意事项。" for i in range(1, 80)]
        result = chunk.split_text("".join(sentences), size=120, overlap=40)

        assert len(result.chunks) >= 2
        for previous, following in zip(result.chunks, result.chunks[1:]):
            overlap = _common_overlap(previous, following)
            assert overlap, "相邻切片应当有重叠"
            # 所有句子都以「第」开头、以「。」结尾；重叠落在句中就会露出别的字符。
            assert overlap.startswith("第"), f"重叠不是从整句开头：{overlap[:20]!r}"
            assert overlap.endswith("。"), f"重叠不是到整句结尾：{overlap[-20:]!r}"

    def test_deterministic(self):
        """同样输入 + 同样参数，必须产出同样的切片（去重依赖这一点）。"""
        text = "".join(f"第{i}句讲的是第{i}个注意事项。" for i in range(1, 200))
        assert chunk.split_text(text).chunks == chunk.split_text(text).chunks

    def test_long_sentence_is_hard_split(self):
        result = chunk.split_text("甲" * 2000, size=400, overlap=50)
        assert result.hard_cuts >= 4
        assert all(len(piece) <= 400 for piece in result.chunks)
        assert sum(len(piece) for piece in result.chunks) == 2000

    def test_long_sentence_prefers_comma(self):
        text = "甲" * 300 + "，" + "乙" * 300 + "。"
        result = chunk.split_text(text, size=400, overlap=0)
        assert result.soft_cuts >= 1
        assert result.hard_cuts == 0
        assert all(len(piece) <= 400 for piece in result.chunks)

    def test_tail_fragment_merges_into_previous(self):
        """「好。」这种尾片要并进上一片，不能单独入库，更不能丢。"""
        sentence = "这是一句完整的话。"   # 9 字
        text = sentence * 23              # 207 字
        result = chunk.split_text(text, size=200, overlap=0, min_chunk=15)

        assert len(result.chunks) == 1
        assert result.merged_tail == 1
        assert result.chunks[0] == text

    def test_invalid_parameters(self):
        for bad in (
            {"size": 0},
            {"size": 100, "overlap": 100},
            {"size": 100, "overlap": -1},
        ):
            try:
                chunk.split_text("正文。", **bad)
            except ValueError:
                continue
            raise AssertionError(f"这些参数应当被拒绝：{bad}")


# ══ 编码 ══════════════════════════════════════════════════════

class TestReadTextAuto:
    def test_gbk_chinese(self, tmp_path):
        path = tmp_path / "gbk.txt"
        path.write_bytes("设备巡检记录".encode("gbk"))

        text, encoding = extract.read_text_auto(path)

        assert text == "设备巡检记录"
        assert encoding == "gb18030"

    def test_utf8_bom_is_stripped(self, tmp_path):
        path = tmp_path / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbf" + "中文".encode("utf-8"))

        text, encoding = extract.read_text_auto(path)

        assert text == "中文"
        assert encoding == "utf-8-sig"
        assert not text.startswith("﻿")

    def test_plain_ascii(self, tmp_path):
        path = tmp_path / "ascii.txt"
        path.write_bytes(b"hello")

        text, encoding = extract.read_text_auto(path)

        assert text == "hello"
        assert encoding == "utf-8"

    def test_utf8_chinese(self, tmp_path):
        path = tmp_path / "utf8.txt"
        path.write_bytes("设备巡检".encode("utf-8"))

        text, encoding = extract.read_text_auto(path)

        assert text == "设备巡检"
        assert encoding == "utf-8"


# ══ 单元格 ════════════════════════════════════════════════════

class TestCell:
    def test_none_is_empty(self):
        assert extract._cell(None) == ""

    def test_integral_float_has_no_tail(self):
        assert extract._cell(2015.0) == "2015"
        assert extract._cell(2015) == "2015"

    def test_newline_is_flattened(self):
        assert extract._cell("第一行\n第二行") == "第一行 第二行"
        assert extract._cell("第一行\r\n第二行") == "第一行 第二行"

    def test_pipe_is_replaced(self):
        assert extract._cell("a|b") == "a／b"

    def test_date_and_datetime(self):
        assert extract._cell(datetime.datetime(2023, 1, 1)) == "2023-01-01"
        assert extract._cell(datetime.datetime(2023, 1, 1, 9, 30)) == "2023-01-01 09:30:00"
        assert extract._cell(datetime.date(2023, 1, 1)) == "2023-01-01"


# ══ 入库载荷与去重 ════════════════════════════════════════════

class _FakeClient:
    """只实现 get，用来钉住去重探测的判定条件。"""

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        return self.hits


class TestStore:
    def test_payload_minimal(self):
        payload = store.make_payload("正文", "笔记", ["长文进库"], "K7/a.txt")
        assert payload == {
            "content": "正文",
            "type": "笔记",
            "tags": ["长文进库"],
            "source": "K7/a.txt",
        }

    def test_optional_ids_only_when_given(self):
        payload = store.make_payload("正文", "笔记", [], None)
        assert "author_id" not in payload
        assert "activity_id" not in payload
        assert "participation_id" not in payload

        payload = store.make_payload(
            "正文", "笔记", [], None, author_id=1, activity_id=2, participation_id=3
        )
        assert payload["author_id"] == 1
        assert payload["activity_id"] == 2
        assert payload["participation_id"] == 3

    def test_type_is_valid_for_k1(self):
        from cli import exp

        payload = store.make_payload("正文", "笔记", [], None)
        assert payload["type"] in exp.EXPERIENCE_TYPES

    def test_find_existing_requires_same_content_and_source(self):
        hits = [
            {
                "exp_id": "exp-1",
                "content": "同样的内容",
                "metadata": {"source": "K7/a.txt"},
            }
        ]
        client = _FakeClient(hits)

        assert store.find_existing(client, "同样的内容", "K7/a.txt") == "exp-1"
        # 正文相同但来源不同 → 不是同一条
        assert store.find_existing(client, "同样的内容", "K7/b.txt") is None
        # 来源相同但正文不同 → 不是同一条
        assert store.find_existing(client, "别的内容", "K7/a.txt") is None

    def test_find_existing_probe_is_truncated(self):
        client = _FakeClient([])
        store.find_existing(client, "甲" * 500, "s")

        path, params = client.calls[0]
        assert path == "/api/experience/keyword"
        assert len(params["q"]) == 120
        assert params["limit"] == 20

    def test_find_existing_survives_probe_failure(self):
        class Broken:
            def get(self, path, **params):
                from cli.client import ApiError, EXIT_UNREACHABLE

                raise ApiError("unreachable", "连不上", EXIT_UNREACHABLE)

        # 探测失败不能挡住入库
        assert store.find_existing(Broken(), "正文", "s") is None


# ══ 转写参数 ══════════════════════════════════════════════════

class TestTranscribeOptions:
    def test_temperature_is_a_list(self):
        """传标量会关掉 fallback 阶梯，复读循环就出不来了。"""
        kwargs = transcribe.build_decode_kwargs(transcribe.TranscribeOptions())
        assert isinstance(kwargs["temperature"], list)
        assert kwargs["temperature"][0] == 0.0
        assert len(kwargs["temperature"]) > 1

    def test_prefix_is_none_or_hotwords_silently_dies(self):
        kwargs = transcribe.build_decode_kwargs(transcribe.TranscribeOptions())
        assert kwargs["prefix"] is None

    def test_hotwords_are_forwarded(self):
        options = transcribe.TranscribeOptions(hotwords="投影仪，场地申请")
        kwargs = transcribe.build_decode_kwargs(options)
        assert kwargs["hotwords"] == "投影仪，场地申请"

    def test_vad_is_on_by_default(self):
        kwargs = transcribe.build_decode_kwargs(transcribe.TranscribeOptions())
        assert kwargs["vad_filter"] is True
        assert kwargs["vad_parameters"]["min_silence_duration_ms"] == 500

    def test_build_hotwords_trims_and_truncates(self):
        assert transcribe.build_hotwords([]) is None
        assert transcribe.build_hotwords([" ", ""]) is None
        assert transcribe.build_hotwords(["投影仪", "场地申请"]) == "投影仪，场地申请"
        assert len(transcribe.build_hotwords(["甲" * 500])) == 120


# ══ 命令行 ════════════════════════════════════════════════════

class TestParser:
    def test_help_formats(self):
        """argparse 会把 help 当 % 格式串；写成「25% 时间」会让 --help 直接崩。"""
        from ingest import run_ingest

        text = run_ingest.build_parser().format_help()

        assert "长文进库" in run_ingest.build_parser().description
        assert "10–25% 时间" in text
