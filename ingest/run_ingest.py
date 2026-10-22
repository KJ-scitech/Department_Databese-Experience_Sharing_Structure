"""
K7 长文进库脚本（完全体）
功能：音频 / 文本 / PPT / Word / PDF / Excel → 切片 → 调用 K1 接口入库
负责人：刘超宇
用法：
  python ingest/run_ingest.py <文件路径> [录音主题提示]
"""

import os
import sys
import re
import requests

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from faster_whisper import WhisperModel
import opencc
from pptx import Presentation
from docx import Document
import pdfplumber
import pandas as pd

API_BASE = "http://localhost:8000"
# 统一用于简繁转换
converter = opencc.OpenCC("t2s")

def transcribe(audio_path, initial_prompt):
    """音频 → 文字"""
    print(f"检测到音频文件，开始转写... (提示：{initial_prompt})")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        audio_path, language="zh", beam_size=5,
        initial_prompt=initial_prompt,
        condition_on_previous_text=False,
    )
    full_text = ""
    for segment in segments:
        full_text += converter.convert(segment.text)
    print(f"转写完成，共 {len(full_text)} 字")
    return full_text

def read_text_file(file_path):
    """纯文本文件 → 文字"""
    print("检测到文本文件，直接读取...")
    with open(file_path, "r", encoding="utf-8") as f:
        text = converter.convert(f.read())
    print(f"读取完成，共 {len(text)} 字")
    return text

def read_pptx_file(file_path):
    """PPT文件 → 文字"""
    print("检测到PPT文件，开始提取文字...")
    prs = Presentation(file_path)
    text = ""
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text += shape.text + "\n"
        if slide.has_notes_slide:
            text += slide.notes_slide.notes_text_frame.text + "\n"
    text = converter.convert(text)
    print(f"提取完成，共 {len(text)} 字")
    return text

def read_docx_file(file_path):
    """Word文件 → 文字"""
    print("检测到Word文件，开始提取文字...")
    doc = Document(file_path)
    text = ""
    for para in doc.paragraphs:
        if para.text.strip():
            text += para.text + "\n"
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    text += cell.text + "\n"
    text = converter.convert(text)
    print(f"提取完成，共 {len(text)} 字")
    return text

def read_pdf_file(file_path):
    """PDF文件 → 文字（支持文字版PDF，扫描版需OCR另处理）"""
    print("检测到PDF文件，开始提取文字...")
    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    text = converter.convert(text)
    print(f"提取完成，共 {len(text)} 字")
    return text

def read_excel_file(file_path):
    """Excel表格 → 文字"""
    print("检测到Excel表格，开始提取...")
    # 读取所有sheet
    dfs = pd.read_excel(file_path, sheet_name=None)
    text = ""
    for sheet_name, df in dfs.items():
        text += f"【工作表：{sheet_name}】\n"
        # 把表格转成纯文本（去掉空值）
        text += df.to_csv(index=False, sep=" | ")
    text = converter.convert(text)
    print(f"提取完成，共 {len(text)} 字")
    return text

def split_text(text, max_len=160):
    """长文 → 切片"""
    sentences = re.split(r"(?<=[。！？；\n])", text)
    chunks, current = [], ""
    for s in sentences:
        if not s.strip():
            continue
        if len(current) + len(s) <= max_len:
            current += s
        else:
            if current:
                chunks.append(current.strip())
            current = s
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if c]

def ingest(chunks):
    """切片 → 调用 K1 接口入库"""
    success = 0
    for i, chunk in enumerate(chunks):
        payload = {
            "content": chunk,
            "type": "笔记",
            "tags": ["长文进库", "K7"],
            "source": "K7-长文进库",
        }
        resp = requests.post(f"{API_BASE}/api/experience", json=payload)
        if resp.status_code == 200:
            success += 1
            print(f"  [第 {i+1}/{len(chunks)} 段] 入库成功")
        else:
            print(f"  [第 {i+1}/{len(chunks)} 段] 入库失败：{resp.status_code}")
    print(f"\n全部完成！成功入库 {success}/{len(chunks)} 段")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python ingest/run_ingest.py <文件路径> [录音主题提示]")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"错误：找不到文件 {input_path}")
        sys.exit(1)

    file_ext = os.path.splitext(input_path)[1].lower()
    
    # 文件类型分类
    text_exts = [".txt", ".md", ".csv"]
    audio_exts = [".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"]
    ppt_exts = [".pptx"]
    docx_exts = [".docx"]
    pdf_exts = [".pdf"]
    excel_exts = [".xlsx", ".xls"]
    old_format_exts = [".doc", ".ppt"]  # 明确拦截老版本格式

    print("=" * 50)
    print(f"K7 长文进库 - 开始处理：{input_path}")
    print("=" * 50)

    if file_ext in text_exts:
        text = read_text_file(input_path)
    elif file_ext in pdf_exts:
        text = read_pdf_file(input_path)
    elif file_ext in ppt_exts:
        text = read_pptx_file(input_path)
    elif file_ext in docx_exts:
        text = read_docx_file(input_path)
    elif file_ext in excel_exts:
        text = read_excel_file(input_path)
    elif file_ext in audio_exts:
        if len(sys.argv) >= 3:
            initial_prompt = sys.argv[2]
        else:
            initial_prompt = "这是一段中文录音，请根据发音准确转写。"
        text = transcribe(input_path, initial_prompt)
    elif file_ext in old_format_exts:
        print(f"错误：不支持老版格式 {file_ext}。")
        print("请用 Office 或 WPS 打开该文件，另存为 .docx 或 .pptx 后再试。")
        sys.exit(1)
    else:
        print(f"错误：不支持的文件格式 {file_ext}。")
        sys.exit(1)

    if not text.strip():
        print("警告：提取出来的文字为空，可能是扫描版PDF或空白文件，终止入库。")
        sys.exit(0)

    chunks = split_text(text)
    print(f"\n切片完成，共 {len(chunks)} 段\n")
    ingest(chunks)
    print("\nK7 完成！")