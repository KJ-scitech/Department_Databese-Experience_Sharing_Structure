# -*- coding: utf-8 -*-
"""K7 长文进库脚本（命令行入口）。

功能：音频 / 文本 / PPT / Word / PDF / Excel → 文字 → 切片 → 调用 K1 接口入库。

用法：
    python -m ingest.run_ingest <文件路径> [选项]      ← 推荐
    python ingest/run_ingest.py <文件路径> [选项]      ← 也支持

`-m` 跑法要求当前目录是仓库根（和 `python -m scripts.xxx` / `python -m cli` 一致）。

常用选项（完整说明见 --help）：
    --dry-run                 只看切片，不入库
    --list-chunks             打印每一片（隐含 --dry-run）
    --limit 3                 只处理前 3 片（三分钟验完整链路）
    --model medium            转写模型降级（默认 large-v3-turbo）
    --speed-check             先试转 90 秒，估算整段要跑多久
    --hotwords "投影仪,场地申请"  术语表（比 initial_prompt 有效得多）
    --transcript-file 文本     跳过转写，直接用现成文字稿入库
    --no-dedupe               关掉入库前的重复探测

★ 首次跑音频会下载模型（large-v3-turbo 约 1.6GB），走 hf-mirror。
"""
import os
import sys

# ── ① HF 镜像 / 关 xet：必须在 huggingface_hub 被导入之前设好 ──
#
# 它导入那一刻把这两个值读成模块常量，之后再设没有任何作用
# （见 docs/开发说明.md 的「HF 下载坑」）。
# 重复写一份是刻意的：这一行必须比下面所有 import 都早。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# ── ② 让两种跑法都能 import 到仓库内的包 ────────────────────
#
# 直接跑 `python ingest/run_ingest.py` 时，sys.path[0] 是 ingest/ 而不是
# 仓库根，`from ingest import ...` / `from cli import ...` 会 ImportError。
# 加 ingest/__init__.py 解决不了——直接跑脚本时它根本不会被执行。
# 补路径是唯一干净的办法，和 tests/conftest.py 的做法一致。
if not __package__:  # 直接跑：__package__ 是 None
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

import argparse  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from scripts import console  # noqa: E402  —— 必须在补路径之后
from cli import client as cli_client  # noqa: E402
from cli import exp as cli_exp  # noqa: E402
from ingest import chunk, config, extract, store, transcribe  # noqa: E402

# 文件类型分流
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}
TEXT_EXTS = {".txt", ".md"}
CSV_EXTS = {".csv"}
PPTX_EXTS = {".pptx"}
DOCX_EXTS = {".docx"}
PDF_EXTS = {".pdf"}
EXCEL_EXTS = {".xlsx"}
# 明确拦截的旧格式：不是「忘了支持」，是给一句能照做的提示
OLD_EXTS = {".doc", ".ppt"}
XLS_EXTS = {".xls"}

ALL_EXTS = (
    AUDIO_EXTS | TEXT_EXTS | CSV_EXTS | PPTX_EXTS | DOCX_EXTS | PDF_EXTS | EXCEL_EXTS
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.run_ingest",
        description="K7 长文进库：文件 → 文字 → 切片 → K1 接口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m ingest.run_ingest 例会.m4a --speed-check --dry-run\n"
            "  python -m ingest.run_ingest 例会.m4a --hotwords \"投影仪,场地申请\"\n"
            "  python -m ingest.run_ingest 纪要.docx --limit 3\n"
            "  python -m ingest.run_ingest 录音.m4a --transcript-file 转写.txt --dry-run\n"
        ),
    )

    parser.add_argument("path", nargs="?", help="要处理的文件")
    parser.add_argument(
        "topic",
        nargs="?",
        help="录音主题提示。★ 只影响第一个 30 秒窗口，术语请用 --hotwords",
    )
    parser.add_argument(
        "--transcript-file",
        help="跳过转写，直接读这份文字稿入库（人工校对过的稿子走这里）",
    )

    # ── 转写 ──────────────────────────────────────────────────
    group = parser.add_argument_group("转写")
    group.add_argument(
        "--model",
        default=config.DEFAULT_MODEL,
        help=f"large-v3-turbo（默认）/ medium / small / large-v3，或本地模型目录",
    )
    group.add_argument(
        "--compute-type",
        default=config.DEFAULT_COMPUTE_TYPE,
        choices=["int8", "int8_float32", "float32"],
        help="纯 CPU 上 int8 最划算（默认）；float32 慢一倍多、内存翻四倍",
    )
    group.add_argument("--beam-size", type=int, default=config.DEFAULT_BEAM_SIZE,
                       help="默认 5。设 1 是最实用的提速杠杆，精度略降")
    group.add_argument("--language", default=config.DEFAULT_LANGUAGE,
                       help="默认强制 zh。自动检测只看前 30 秒，判错会静默劣化整场")
    group.add_argument("--hotwords", help="术语表，逗号分隔，如 \"投影仪,场地申请\"")
    group.add_argument("--hotwords-file", help="术语表文件，一行一个，# 开头是注释")
    group.add_argument("--no-context", action="store_true",
                       help="condition_on_previous_text=False。某场录音出现复读时用它")
    group.add_argument("--no-vad", action="store_true", help="关掉 VAD（不推荐）")
    group.add_argument("--vad-threshold", type=float, default=config.DEFAULT_VAD_THRESHOLD,
                       help="VAD 阈值，默认 0.5。噪声大时调高到 0.6，代价是漏掉小声说话的人")
    group.add_argument("--chunk-minutes", type=int, default=config.DEFAULT_CHUNK_MINUTES,
                       help="长录音按静音切成这么大的块，默认 15 分钟")
    group.add_argument("--speed-check", action="store_true",
                       help="先试转 90 秒，报告实时率和整段预计耗时")
    group.add_argument("--word-timestamps", action="store_true",
                       help="输出带时间戳的 .timestamped.txt（多花 10–25%% 时间）")
    group.add_argument("--gain-normalize", action="store_true",
                       help="把峰值拉到 0.95（默认不动波形）")
    group.add_argument("--cpu-threads", type=int, default=0,
                       help="交给 ctranslate2 自动决定（默认 0）；不要超过物理核数")

    # ── 切片 ──────────────────────────────────────────────────
    group = parser.add_argument_group("切片")
    group.add_argument("--chunk-size", type=int, default=config.CHUNK_SIZE,
                       help=f"每片目标字数，默认 {config.CHUNK_SIZE}")
    group.add_argument("--overlap", type=int, default=config.CHUNK_OVERLAP,
                       help=f"相邻切片重叠字数，默认 {config.CHUNK_OVERLAP}（按整句取）")

    # ── 入库 ──────────────────────────────────────────────────
    group = parser.add_argument_group("入库")
    group.add_argument("--base", help="服务地址。默认读 KB_API_BASE，再默认 http://127.0.0.1:8000")
    group.add_argument("--password",
                       default=config.SHARED_PASSWORD or None,
                       help="服务口令。默认读 .env 的 KB_SHARED_PASSWORD；本机回环地址不需要")
    group.add_argument("--type", dest="type_", choices=cli_exp.EXPERIENCE_TYPES,
                       default="笔记", help="经验类型，默认 笔记")
    group.add_argument("--tags", default="长文进库,K7", help="标签，逗号分隔")
    group.add_argument("--source", help="来源。默认 K7-长文进库/<文件名>")
    group.add_argument("--author-id", type=int, help="关联 member.id")
    group.add_argument("--activity-id", type=int, help="关联 activity.id")
    group.add_argument("--participation-id", type=int, help="关联 participation.id")
    group.add_argument("--no-dedupe", action="store_true",
                       help="不做入库前的重复探测（默认会跳过已在库里的切片）")
    group.add_argument("--dry-run", action="store_true", help="只看切片，不发请求")
    group.add_argument("--list-chunks", action="store_true",
                       help="打印每一片（隐含 --dry-run）")
    group.add_argument("--limit", type=int, help="只处理前 N 片（冒烟测试用）")
    group.add_argument("--yes", action="store_true", help="超过 50 片时不问确认")
    group.add_argument("--quiet", action="store_true", help="只打错误和最终摘要")

    return parser


def read_hotwords_file(path: str) -> list[str]:
    terms: list[str] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms.extend(t for t in re.split(r"[,，]", line) if t.strip())
    return terms


def extract_file(path: Path) -> extract.Extracted:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTS:
        return extract.read_text_file(path)
    if suffix in CSV_EXTS:
        return extract.read_csv_file(path)
    if suffix in PPTX_EXTS:
        return extract.read_pptx_file(path)
    if suffix in DOCX_EXTS:
        return extract.read_docx_file(path)
    if suffix in PDF_EXTS:
        return extract.read_pdf_file(path)
    if suffix in EXCEL_EXTS:
        return extract.read_excel_file(path)
    raise ValueError(f"不支持的文件格式 {suffix}")


def write_timestamps(path: Path, timestamps) -> Path:
    out = path.with_suffix(path.suffix + ".timestamped.txt")
    with open(out, "w", encoding="utf-8") as f:
        for start, end, text in timestamps:
            f.write(f"[{_fmt_seconds(start)} --> {_fmt_seconds(end)}] {text.strip()}\n")
    return out


def _fmt_seconds(value: float) -> str:
    minutes, seconds = divmod(max(value, 0.0), 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
    return f"{minutes:02d}:{seconds:06.3f}"


def parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,，]", raw) if t.strip()]


def _old_format_message(suffix: str) -> str:
    if suffix in XLS_EXTS:
        return (
            "错误：openpyxl 读不了老版 .xls。\n"
            "请用 Excel 或 WPS 打开，另存为 .xlsx 后再试。"
        )
    return (
        f"错误：不支持老版格式 {suffix}。\n"
        "请用 Office 或 WPS 打开该文件，另存为 .docx 或 .pptx 后再试。"
    )


def main(argv=None) -> int:
    console.setup()
    parser = build_parser()
    args = parser.parse_args(argv)

    def log(message: str = "") -> None:
        if not args.quiet:
            print(message)

    input_path = Path(args.path) if args.path else None
    transcript_path = Path(args.transcript_file) if args.transcript_file else None

    if input_path is None and transcript_path is None:
        parser.print_usage()
        print("错误：至少要给一个文件路径（或用 --transcript-file）。")
        return cli_client.EXIT_USAGE

    for candidate in (input_path, transcript_path):
        if candidate is not None and not candidate.exists():
            print(f"错误：找不到文件 {candidate}")
            return cli_client.EXIT_USAGE

    if input_path is not None and input_path.is_dir():
        candidates = sorted(
            p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() in ALL_EXTS
        )
        print(f"错误：{input_path} 是目录，请指定具体文件。")
        if candidates:
            print("这个目录里可以处理的文件：")
            for candidate in candidates[:20]:
                print(f"  {candidate.name}")
            if len(candidates) > 20:
                print(f"  …还有 {len(candidates) - 20} 个")
        return cli_client.EXIT_USAGE

    if input_path is not None:
        suffix = input_path.suffix.lower()
        if suffix in OLD_EXTS or suffix in XLS_EXTS:
            print(_old_format_message(suffix))
            return cli_client.EXIT_USAGE

    started = time.perf_counter()
    timings: dict[str, float] = {}

    # ── 1. 取文字 ────────────────────────────────────────────
    timings["准备"] = time.perf_counter() - started
    extract_started = time.perf_counter()

    if transcript_path is not None:
        try:
            extracted = extract.read_text_file(transcript_path)
        except ImportError as exc:
            print(f"错误：缺少依赖 {exc.name}。先装：pip install -r requirements.txt")
            return cli_client.EXIT_ERROR
        text = extracted.text
        log(f"读取文字稿：{transcript_path}（{extracted.detail}）")
        source_path = input_path or transcript_path

    elif input_path.suffix.lower() in AUDIO_EXTS:
        terms: list[str] = []
        if args.hotwords:
            terms.extend(re.split(r"[,，]", args.hotwords))
        if args.hotwords_file:
            terms.extend(read_hotwords_file(args.hotwords_file))

        options = transcribe.TranscribeOptions(
            model=args.model,
            compute_type=args.compute_type,
            beam_size=args.beam_size,
            language=args.language,
            hotwords=transcribe.build_hotwords(terms),
            initial_prompt=args.topic,
            vad_filter=not args.no_vad,
            vad_threshold=args.vad_threshold,
            condition_on_previous_text=not args.no_context,
            word_timestamps=args.word_timestamps,
            gain_normalize=args.gain_normalize,
            chunk_minutes=args.chunk_minutes,
            speed_check=args.speed_check,
            cpu_threads=args.cpu_threads,
        )
        if options.hotwords:
            log(f"[转写] hotwords：{options.hotwords}")
        try:
            result = transcribe.transcribe_audio(input_path, options, log=log)
        except ImportError as exc:
            print(f"错误：缺少依赖 {exc.name}。先装：pip install -r requirements.txt")
            return cli_client.EXIT_ERROR
        text = result.text
        source_path = input_path
        if args.word_timestamps and result.timestamps:
            out = write_timestamps(input_path, result.timestamps)
            log(f"[转写] 时间戳已写到 {out}")

    else:
        try:
            extracted = extract_file(input_path)
        except ImportError as exc:
            print(f"错误：缺少依赖 {exc.name}。先装：pip install -r requirements.txt")
            return cli_client.EXIT_ERROR
        except ValueError as exc:
            print(f"错误：{exc}")
            return cli_client.EXIT_USAGE
        text = extracted.text
        log(f"提取完成：{input_path}（{extracted.detail}）")
        source_path = input_path

    timings["提取/转写"] = time.perf_counter() - extract_started

    if not text.strip():
        suffix = source_path.suffix.lower()
        if suffix in PDF_EXTS:
            print("失败：这个 PDF 提取不出文字，多半是扫描版（没有文字层）。")
            print("      当前脚本不含 OCR。请先做 OCR，或用 --transcript-file 提供文字稿。")
        else:
            print("失败：文件里没有可提取的文字（空文件，或格式里没有文字层）。")
        return cli_client.EXIT_ERROR

    # ── 2. 切片 ──────────────────────────────────────────────
    chunk_started = time.perf_counter()
    try:
        split = chunk.split_text(text, size=args.chunk_size, overlap=args.overlap)
    except ValueError as exc:
        print(f"错误：{exc}")
        return cli_client.EXIT_USAGE

    chunks = split.chunks
    if not chunks:
        print("失败：切片后没有任何内容。")
        return cli_client.EXIT_ERROR

    if args.limit is not None:
        if args.limit < 1:
            print("错误：--limit 必须是正整数。")
            return cli_client.EXIT_USAGE
        chunks = chunks[: args.limit]

    timings["切片"] = time.perf_counter() - chunk_started

    lengths = [len(c) for c in chunks]
    log(
        f"切片完成：{len(chunks)} 段"
        f"（最短 {min(lengths)}、中位 {statistics.median(lengths):.0f}、最长 {max(lengths)}）"
    )
    log(
        f"        长句软断 {split.soft_cuts} 处、硬断 {split.hard_cuts} 处、"
        f"尾片合并 {split.merged_tail} 处"
    )
    if split.hard_cuts:
        log("        注意：硬断是唯一会切在句子中间的情况，来源多半是 PDF 里没有标点的长段")

    # ── 3. 看一眼（dry-run）──────────────────────────────────
    if args.dry_run or args.list_chunks:
        if args.list_chunks:
            for index, piece in enumerate(chunks, start=1):
                log(f"  [{index}/{len(chunks)}] {len(piece)} 字 | {piece[:40]}…")
        else:
            for index, piece in enumerate(chunks[:3], start=1):
                log(f"\n--- 第 {index} 片（{len(piece)} 字）---\n{piece}")
            if len(chunks) > 3:
                log(f"\n…其余 {len(chunks) - 3} 片省略（用 --list-chunks 可以全看）")
        log(f"\n将会入库 {len(chunks)} 条（--dry-run，没有发任何请求）")
        return cli_client.EXIT_OK

    # ── 4. 入库 ──────────────────────────────────────────────
    if len(chunks) > 50 and not args.yes and sys.stdin.isatty():
        answer = input(f"将入库 {len(chunks)} 条，确认？[y/N] ")
        if answer.strip().lower() not in ("y", "yes", "是"):
            log("已取消。")
            return cli_client.EXIT_OK

    tags = parse_tags(args.tags)
    source = args.source or f"K7-长文进库/{source_path.name}"

    log(f"开始入库：{len(chunks)} 段 → {args.base or 'KB_API_BASE / 127.0.0.1:8000'}")
    api = store.make_client(args.base, args.password)

    stats = store.ingest_chunks(
        api,
        chunks,
        type_=args.type_,
        tags=tags,
        source=source,
        author_id=args.author_id,
        activity_id=args.activity_id,
        participation_id=args.participation_id,
        skip_existing=not args.no_dedupe,
        log=log,
    )
    timings["入库"] = stats.elapsed

    # ── 5. 摘要 ──────────────────────────────────────────────
    print("")
    print("=" * 56)
    print(
        f"完成：成功 {stats.success}，跳过 {stats.skipped}，"
        f"失败 {len(stats.failed)}，共 {stats.total} 段"
    )
    if stats.aborted:
        print(f"中途停止：{stats.aborted}")
    timing_text = "，".join(f"{name} {seconds:.1f}s" for name, seconds in timings.items())
    print(f"耗时：{timing_text}，总计 {time.perf_counter() - started:.1f}s")
    if stats.exp_ids:
        print(f"exp_id：{stats.exp_ids[0]} … {stats.exp_ids[-1]}")

    if stats.failed:
        codes = {item["code"] for item in stats.failed}
        if "rejected" in codes:
            return cli_client.EXIT_REJECTED
        if "unreachable" in codes:
            return cli_client.EXIT_UNREACHABLE
        return cli_client.EXIT_ERROR
    return cli_client.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
