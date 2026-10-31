# -*- coding: utf-8 -*-
"""文件 → 文字。

★ **所有重型第三方库都在函数里惰性导入。**

  本机实测 import 耗时（热缓存）：opencc 0.10s、python-docx 0.35s、
  python-pptx 0.74s、pdfplumber 0.84s、pandas 2.83s——合计约 5.6s，
  M710q 上翻 2–3 倍很正常。如果放顶层，读一个 .txt 要先等十几秒，
  `--help` 和 `--dry-run` 也要付这个钱。惰性导入之后，纯文本路径
  只需要标准库。

★ 表格统一输出成「a | b | c」一行一条。xlsx 和 csv 走同一形状——
  同一批数据换个后缀，入库文本的形状不该跟着变。
"""
from __future__ import annotations

import csv as _csv
import datetime as _datetime
import io
import re
from dataclasses import dataclass
from pathlib import Path

from ingest import config  # noqa: F401  —— 保证 HF 环境变量先于下载库设好


@dataclass
class Extracted:
    """提取结果。detail 是给人看的说明（编码、格式之类的）。"""

    text: str
    detail: str = ""


# ── 编码 ──────────────────────────────────────────────────────

# BOM 必须放在「猜编码」之前判断，否则 UTF-8-SIG 的 BOM 会作为
# 一个看不见的 ﻿ 留在正文最前面，跟着进库、进检索结果。
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def read_text_auto(path: str | Path) -> tuple[str, str]:
    """读文本文件，返回 (文本, 实际使用的编码)。

    顺序有讲究：UTF-8 先试（它对自己以外的字节序列几乎必然抛错），
    再试 gb18030（它是 GBK/GB2312 的超集，几乎能解出任何字节，
    所以**必须放最后**，否则会把本来的 UTF-8 文本解成乱码还不报错）。

    ★ 不引入 charset-normalizer：它是 requests 的传递依赖，
      把传递依赖当直接依赖用，会在上游换实现时静默炸掉。
      实际需要区分的只有 UTF-8 和 GB18030 两种。
    """
    raw = Path(path).read_bytes()

    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace"), encoding

    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace"), "utf-8（有损）"


# ── 繁简 ──────────────────────────────────────────────────────

_CC = None


def to_simplified(text: str) -> str:
    """繁 → 简。OpenCC 构造一次约 0.1s，用模块级单例，别每片都构造。"""
    global _CC
    if not text:
        return text
    if _CC is None:
        import opencc

        _CC = opencc.OpenCC("t2s")
    return _CC.convert(text)


# ── 各格式 ────────────────────────────────────────────────────

def read_text_file(path: str | Path) -> Extracted:
    text, encoding = read_text_auto(path)
    return Extracted(to_simplified(text), f"编码 {encoding}")


def read_csv_file(path: str | Path) -> Extracted:
    """CSV → 每行一条「a | b | c」。

    用标准库 csv 模块，它能正确处理引号里的逗号和换行；
    按行 split(",") 是做不到的。
    """
    text, encoding = read_text_auto(path)
    try:
        rows = list(_csv.reader(io.StringIO(text)))
    except _csv.Error:
        # 编码或引号坏到 csv 都解析不了时，退回纯文本——
        # 宁可形状差一点，也不要让整份文件进不了库。
        return Extracted(to_simplified(text), f"编码 {encoding}；CSV 解析失败，按纯文本处理")

    lines: list[str] = []
    for row in rows:
        cells = [_cell(value) for value in row]
        while cells and not cells[-1]:
            cells.pop()  # 去掉尾部空列
        if not any(cells):
            continue
        lines.append(" | ".join(cells))

    return Extracted(to_simplified("\n".join(lines)), f"编码 {encoding}")


def read_excel_file(path: str | Path) -> Extracted:
    """xlsx → 每行一条「a | b | c」。

    ★ 用 openpyxl 而不是 pandas。pandas 的 `to_csv(sep=" | ")` 有四个真问题：
      空单元格变成字面量 "nan"、单元格里的换行会把一行拆成两行、
      整数列有 NaN 时整列变 float（20230101 → 20230101.0）、
      多字符 sep 的行为随版本变化。openpyxl 里这些值本来就是
      None / int / datetime，不需要猜。
      顺带把 pandas 和它拖的 numpy 依赖从 ingest 这条线上拿掉。
    """
    import openpyxl

    # read_only=True：大表按行流式读，不把整本载入内存。
    # data_only=True：取公式的**计算结果**，不是 "=SUM(...)" 文本。
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f"【工作表：{sheet.title}】")
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell(value) for value in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not any(cells):
                    continue
                lines.append(" | ".join(cells))
    finally:
        workbook.close()

    return Extracted(to_simplified("\n".join(lines)), "xlsx")


def read_pptx_file(path: str | Path) -> Extracted:
    """PPT → 文字（每页正文 + 表格 + 备注）。"""
    from pptx import Presentation

    presentation = Presentation(path)
    lines: list[str] = []

    for index, slide in enumerate(presentation.slides, start=1):
        lines.append(f"【第 {index} 页】")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                if shape.text.strip():
                    lines.append(shape.text)
            elif getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [_cell(cell.text) for cell in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"【备注】{notes}")

    return Extracted(to_simplified("\n".join(lines)), "pptx")


def read_docx_file(path: str | Path) -> Extracted:
    """Word → 文字（段落 + 表格）。"""
    from docx import Document

    document = Document(path)
    lines: list[str] = []

    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            lines.append(paragraph.text)

    for table in document.tables:
        for row in table.rows:
            cells = [_cell(cell.text) for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))

    return Extracted(to_simplified("\n".join(lines)), "docx")


def read_pdf_file(path: str | Path) -> Extracted:
    """PDF → 文字（只支持有文字层的版本）。"""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                lines.append(page_text)

    return Extracted(to_simplified("\n".join(lines)), "pdf")


# ── 单元格格式化 ──────────────────────────────────────────────

def _cell(value) -> str:
    """把单元格值变成一行里的一格。

    ★ 单元格内的换行必须压成空格：否则一行表格会变成两行，
      「|」分隔的结构就坏了。单元格里本来就有「|」的话换成全角，
      同理——它是分隔符，不能出现在字段里。
    """
    if value is None:
        return ""

    if isinstance(value, bool):
        # bool 是 int 的子类，必须在 int/float 分支之前判掉。
        return str(value)

    if isinstance(value, _datetime.datetime):
        # 纯日期（时间部分是 00:00:00）只写日期，省掉一串没用的 0。
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, _datetime.date):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))  # 2015.0 → 2015
        return f"{value:g}"         # 避免 0.30000000000000004

    text = str(value)
    text = re.sub(r"[\r\n]+", " ", text)
    return text.replace("|", "／").strip()
