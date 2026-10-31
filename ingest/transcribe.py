# -*- coding: utf-8 -*-
"""音频 → 文字。

★ 模块顶部 `from ingest import config` 不是多余的一行：
  它保证 HF_ENDPOINT / HF_HUB_DISABLE_XET 在任何 huggingface_hub
  导入之前设好。faster_whisper 是惰性导入的，所以只要这个模块被导入过，
  顺序就是对的。

━━━ 解码参数的取舍（改之前先读）━━━━━━━━━━━━━━━━━━━━━━━━━━━━

· `vad_filter=True`：长录音的静音段是幻觉的第一大来源（Whisper 在纯静音
  窗口会稳定输出「谢谢观看」这类训练集残留）。VAD 从**音频**层面删掉静音，
  比事后过滤文本可靠，纯 CPU 上还省时间。

· `min_silence_duration_ms=500`（默认 2000）：默认值是播客场景——
  要求连续静音超过 2 秒才切段。会议里两人之间 1–2 秒的停顿非常多，
  用默认值等于把这些停顿连同幻觉窗口一起留着。

· `speech_pad_ms=400`（保持默认，别调小）：vad.py 对相邻段做 padding 时，
  如果静音长度 < 2*speech_pad_ms，会把这段静音**对半分**给两侧、不重叠。
  所以调大 pad 在窄间隙时不会更好地保护字头，只会在宽间隙时
  把已经删掉的静音又加回来。调到 300 以下才会真的吃字头。

· `hotwords` 而不是 `initial_prompt`：initial_prompt 只在**第一个**
  30 秒窗口生效，而且 Whisper 把它当「上文的转写结果」来接续，
  不是当系统提示词。灌术语要用 hotwords——它在**每个**窗口都注入。

· `prefix=None` 必须保持：faster-whisper 的 get_prompt() 里，
  hotwords 只在 `hotwords and not prefix` 时生效——设了 prefix，
  hotwords 会**静默失效**。

· `temperature` 必须是**列表**。传标量 0 会关掉整个 fallback 阶梯，
  那不是「更确定」，而是模型卡在复读循环里出不来。

· `condition_on_previous_text=True`：让术语、人名在窗口之间保持一致。
  原来设 False 是为了防复读，但现在开了 VAD（静音窗口没了）、
  prompt_reset_on_temperature 会在 fallback 时重置 prompt（内建自愈），
  而且我们按 15 分钟切块（污染最多毁一块）。留 --no-context 做逃生门。

· `word_timestamps=False`：开了要多跑一遍 cross-attention + DTW，
  纯 CPU 上多花 10–25%。人工复核可疑段落时才用 --word-timestamps。
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from ingest import config

SAMPLE_RATE = 16000

# 低于这个峰值（约 -28 dBFS）就提醒电平偏低；接近满刻度提醒削顶。
_LOW_PEAK = 0.04
_CLIP_PEAK = 0.999


@dataclass
class TranscribeOptions:
    """一次转写要用到的全部开关。默认值就是推荐值。"""

    model: str = config.DEFAULT_MODEL
    compute_type: str = config.DEFAULT_COMPUTE_TYPE
    beam_size: int = config.DEFAULT_BEAM_SIZE
    language: str = config.DEFAULT_LANGUAGE
    hotwords: str | None = None
    # 只影响第一个 30 秒窗口。术语请用 hotwords。
    initial_prompt: str | None = None
    vad_filter: bool = True
    vad_threshold: float = config.DEFAULT_VAD_THRESHOLD
    condition_on_previous_text: bool = True
    word_timestamps: bool = False
    gain_normalize: bool = False
    chunk_minutes: int = config.DEFAULT_CHUNK_MINUTES
    speed_check: bool = False
    cpu_threads: int = 0


@dataclass
class DecodeStats:
    """解码过程的健康度统计。

    fallback 段占比高（> 5%）说明模型对这段音频不自信，
    再调 compression_ratio_threshold 才有依据——不要凭感觉调。
    """

    segments: int = 0
    fallback_segments: int = 0
    low_logprob: int = 0
    high_compression: int = 0
    temperatures: list[float] = field(default_factory=list)
    logprobs: list[float] = field(default_factory=list)
    compression_ratios: list[float] = field(default_factory=list)

    def add(self, segment) -> None:
        self.segments += 1
        temperature = float(getattr(segment, "temperature", 0.0) or 0.0)
        self.temperatures.append(temperature)
        if temperature > 0:
            self.fallback_segments += 1

        logprob = getattr(segment, "avg_logprob", None)
        if logprob is not None:
            self.logprobs.append(float(logprob))
            if float(logprob) < -1.0:
                self.low_logprob += 1

        ratio = getattr(segment, "compression_ratio", None)
        if ratio is not None:
            self.compression_ratios.append(float(ratio))
            if float(ratio) > 2.4:
                self.high_compression += 1

    def summary(self) -> str:
        if not self.segments:
            return "解码统计：没有产生任何语音段"

        fallback = self.fallback_segments / self.segments * 100
        parts = [f"段数 {self.segments}", f"fallback 段 {self.fallback_segments}（{fallback:.1f}%）"]
        if self.logprobs:
            parts.append(f"avg_logprob 中位数 {statistics.median(self.logprobs):.2f}")
        if self.compression_ratios:
            ordered = sorted(self.compression_ratios)
            p75 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))]
            parts.append(f"compression_ratio 上四分位 {p75:.2f}")
        return "解码统计：" + " | ".join(parts)


@dataclass
class TranscribeResult:
    text: str
    duration: float = 0.0
    duration_after_vad: float = 0.0
    rtf: float = 0.0
    model_seconds: float = 0.0
    stats: DecodeStats = field(default_factory=DecodeStats)
    # (起始秒, 结束秒, 文本)；始终按段收集，只有 --word-timestamps 时才落盘
    timestamps: list[tuple[float, float, str]] = field(default_factory=list)


def build_hotwords(terms) -> str | None:
    """术语列表 → hotwords 串。

    越短越好：hotwords 和上文共享 223 个 token 的预算，塞太多会把
    模型「带偏」——它可能把没说的词也写出来。经验值是 120 字以内。

    更重要的流程建议：第一轮先不加 hotwords，跑一遍基线，从错例里
    攒术语表，第二轮再加。hotwords 是针对性纠错工具，不是越全越好。
    """
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.replace("，", ",").split(",")]
    cleaned = [str(t).strip() for t in terms if str(t).strip()]
    if not cleaned:
        return None
    joined = "，".join(cleaned)
    return joined[:120] or None


def build_decode_kwargs(options: TranscribeOptions) -> dict:
    """TranscribeOptions → faster_whisper.transcribe() 的参数。

    单独拆出来是为了 tests/test_ingest.py 能在不加载模型的情况下
    把几个「传错了会静默失效」的参数钉死（temperature 列表、prefix=None）。
    """
    return dict(
        language=options.language,
        task="transcribe",
        beam_size=options.beam_size,
        best_of=options.beam_size,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.0,
        no_repeat_ngram_size=0,
        # ★ 必须是列表：标量 0 会关掉 fallback 阶梯。
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        # ★ 别调高：远场录音里小声说话的人 no_speech_prob 本来就高，
        #   调到 0.8 会把真实发言整段吞掉——静默丢数据比多几段幻觉危险。
        no_speech_threshold=0.6,
        condition_on_previous_text=options.condition_on_previous_text,
        prompt_reset_on_temperature=0.5,
        initial_prompt=options.initial_prompt,
        # ★ 必须留 None：设了 prefix，hotwords 会静默失效。
        prefix=None,
        hotwords=options.hotwords,
        suppress_blank=True,
        suppress_tokens=[-1],
        without_timestamps=False,
        max_initial_timestamp=1.0,
        word_timestamps=options.word_timestamps,
        # ★ 别开：会在每个段重新做语言检测，更慢，还可能中途跳语言。
        multilingual=False,
        vad_filter=options.vad_filter,
        vad_parameters=dict(
            threshold=options.vad_threshold,
            min_speech_duration_ms=250,   # 咳嗽、桌椅声不进解码
            min_silence_duration_ms=500,
            speech_pad_ms=400,            # 别调小，见模块开头
            max_speech_duration_s=float("inf"),
        ),
        # 开了 VAD 之后音频里已经没有长静音，这个参数没东西可跳。
        hallucination_silence_threshold=None,
        log_progress=False,
    )


def _load_model(options: TranscribeOptions, log):
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    model = WhisperModel(
        options.model,
        device="cpu",
        compute_type=options.compute_type,
        cpu_threads=options.cpu_threads,
        # 显式给缓存目录：不依赖 HF_HOME 是否被子进程继承。
        download_root=config.HF_HOME,
    )
    elapsed = time.perf_counter() - started
    log(f"[模型] {options.model}（{options.compute_type}，CPU）加载完成，用时 {elapsed:.1f}s")
    return model, elapsed


def _decode_audio(path: str | Path):
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def audio_diagnostics(audio) -> dict:
    """录音电平诊断。

    ★ 明确**不做降噪**：仓库零 ffmpeg，不应引入系统依赖；Python 侧降噪
      会拖进 numba/scipy，而且降噪过度会吃掉辅音——中文对声母很敏感，
      实测经常是负收益。这里只做诊断，让「录音本身有问题」能早点说出来。
    """
    import numpy as np

    if len(audio) == 0:
        return {"peak": 0.0, "rms": 0.0, "warnings": ["音频是空的"]}

    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    warnings: list[str] = []

    if peak < _LOW_PEAK:
        dbfs = 20 * math.log10(peak) if peak > 0 else -120
        warnings.append(f"录音电平偏低（峰值 {peak:.3f}，约 {dbfs:.0f} dBFS），转写可能漏字")
    if peak >= _CLIP_PEAK:
        warnings.append("录音有削顶（峰值已到满刻度），削掉的部分无法恢复")

    return {"peak": peak, "rms": rms, "warnings": warnings}


def normalize_gain(audio, target: float = 0.95):
    """把峰值拉到 target。只在 --gain-normalize 时用，默认不动波形。"""
    import numpy as np

    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 0 or peak >= target:
        return audio, 1.0
    factor = target / peak
    return (audio * factor).astype(np.float32), factor


# ── 长录音切块 ────────────────────────────────────────────────

def _silence_gaps(audio) -> list[tuple[int, int]]:
    """用 VAD 找出静音间隙（样本下标）。

    Silero VAD 模型是 faster-whisper 包内自带的，不需要下载。
    失败时返回空列表，调用方会退回按固定时长切。
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    try:
        spans = get_speech_timestamps(
            audio,
            vad_options=VadOptions(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=500,
                speech_pad_ms=400,
            ),
            sampling_rate=SAMPLE_RATE,
        )
    except Exception:
        return []

    gaps: list[tuple[int, int]] = []
    previous_end = 0
    for span in spans:
        if span["start"] > previous_end:
            gaps.append((previous_end, span["start"]))
        previous_end = span["end"]
    if previous_end < len(audio):
        gaps.append((previous_end, len(audio)))
    return gaps


def _pick_cut(gaps, target: int, tolerance: int) -> int | None:
    """在 target 附近挑最长的静音间隙，返回中点。"""
    best = None
    for start, end in gaps:
        middle = (start + end) // 2
        if abs(middle - target) > tolerance:
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    return None if best is None else (best[0] + best[1]) // 2


def plan_chunks(audio, chunk_seconds: float) -> list[tuple[int, int]]:
    """把长音频切成若干块，尽量切在静音里。

    ★ 为什么不用 transcribe(clip_timestamps=...)：
      faster-whisper 的 transcribe() 里 VAD 的分支条件是
      `if vad_filter and clip_timestamps == "0"`——一旦传了
      clip_timestamps，**vad_filter 会被静默忽略**。
      所以切块必须自己做，切完把 ndarray 传进去（ndarray 会跳过解码）。
    """
    total = len(audio)
    step = int(chunk_seconds * SAMPLE_RATE)
    if total <= step:
        return [(0, total)]

    gaps = _silence_gaps(audio)
    boundaries = [0]
    target = step
    # 最后留 30 秒以上的余量，避免切出一个超短尾块。
    while target < total - step * 0.5:
        cut = _pick_cut(gaps, target, tolerance=int(90 * SAMPLE_RATE))
        if cut is None or cut <= boundaries[-1] + int(30 * SAMPLE_RATE):
            cut = target
        boundaries.append(cut)
        target = cut + step
    boundaries.append(total)

    pairs = list(zip(boundaries, boundaries[1:]))
    # 尾块太短就并进前一块（60 秒以下）。
    if len(pairs) >= 2 and (pairs[-1][1] - pairs[-1][0]) < 60 * SAMPLE_RATE:
        pairs[-2] = (pairs[-2][0], pairs[-1][1])
        pairs.pop()
    return pairs


# ── 主流程 ────────────────────────────────────────────────────

def transcribe_audio(path: str | Path, options: TranscribeOptions, log=print) -> TranscribeResult:
    """转写一个音频文件。

    `log` 可替换成 quiet 版本的函数；进度和统计都走它。
    """
    started = time.perf_counter()
    audio = _decode_audio(path)
    duration = len(audio) / SAMPLE_RATE

    diagnostics = audio_diagnostics(audio)
    log(
        f"[音频] 时长 {duration / 60:.1f} 分钟，峰值 {diagnostics['peak']:.3f}，"
        f"RMS {diagnostics['rms']:.4f}"
    )
    for warning in diagnostics["warnings"]:
        log(f"[注意] {warning}")

    if options.gain_normalize:
        audio, factor = normalize_gain(audio)
        if factor != 1.0:
            log(f"[音频] 已做增益归一化，放大 {factor:.2f} 倍")
        else:
            log("[音频] 增益归一化：不需要调整")

    model, model_seconds = _load_model(options, log)
    kwargs = build_decode_kwargs(options)

    # ── 速度自检 ──────────────────────────────────────────────
    #
    # M710q 的 CPU 型号仓库里没有记载，large-v3-turbo 可能跑不动。
    # 先花一分钟试转前 90 秒，把「预计要跑三小时」在开始之前说出来。
    #
    # ★ segments 是生成器，不消费完计时就是假的。
    if options.speed_check and duration > 180:
        probe_seconds = 90
        probe = audio[: probe_seconds * SAMPLE_RATE]
        log(f"[自检] 试转前 {probe_seconds} 秒，用于估算整段耗时…")
        probe_started = time.perf_counter()
        segments, _ = model.transcribe(probe, **kwargs)
        probe_text = "".join(segment.text for segment in segments)
        probe_elapsed = time.perf_counter() - probe_started
        rtf = probe_elapsed / probe_seconds
        log(
            f"[自检] 试转 {probe_seconds}s 音频耗时 {probe_elapsed:.1f}s → "
            f"实时率 {rtf:.2f}（{1 / rtf:.1f}x 实时），转出 {len(probe_text)} 字"
        )
        log(f"[自检] 整段 {duration / 60:.1f} 分钟预计需要 {rtf * duration / 60:.1f} 分钟")
        if rtf > 1.0:
            log("[自检] 这个速度追不上实时，建议降级：--beam-size 1 → --model medium → --model small")
        elif rtf > 0.5:
            log("[自检] 能跑，但建议挂夜里。想快一点用 --beam-size 1")

    # ── 分块转写 ──────────────────────────────────────────────
    chunks = plan_chunks(audio, options.chunk_minutes * 60)
    if len(chunks) > 1:
        log(f"[转写] 按 {options.chunk_minutes} 分钟切块，共 {len(chunks)} 块")

    parts: list[str] = []
    stats = DecodeStats()
    timestamps: list[tuple[float, float, str]] = []
    duration_after_vad = 0.0
    transcribe_started = time.perf_counter()
    recent: list[float] = []

    for index, (start, end) in enumerate(chunks, start=1):
        chunk_seconds = (end - start) / SAMPLE_RATE
        chunk_started = time.perf_counter()
        segments, info = model.transcribe(audio[start:end], **kwargs)

        piece: list[str] = []
        for segment in segments:
            piece.append(segment.text)
            stats.add(segment)
            timestamps.append(
                (
                    start / SAMPLE_RATE + float(segment.start),
                    start / SAMPLE_RATE + float(segment.end),
                    segment.text,
                )
            )

        elapsed = time.perf_counter() - chunk_started
        recent.append(elapsed)
        recent = recent[-20:]

        kept = getattr(info, "duration_after_vad", None)
        if kept:
            duration_after_vad += float(kept)

        text = "".join(piece)
        parts.append(text)

        if len(chunks) > 1:
            avg = sum(recent) / len(recent)
            remaining = sum((e - s) for s, e in chunks[index:]) / SAMPLE_RATE
            log(
                f"[转写 {index}/{len(chunks)}] {chunk_seconds / 60:.1f} 分钟音频，"
                f"用时 {elapsed:.1f}s，实时率 {elapsed / max(chunk_seconds, 1e-6):.2f}，"
                f"预计剩余 {avg / max(chunk_seconds, 1e-6) * remaining / 60:.1f} 分钟"
            )

    transcribe_elapsed = time.perf_counter() - transcribe_started
    text = "".join(parts)

    log(stats.summary())
    if duration_after_vad:
        log(f"[转写] VAD 保留了 {duration_after_vad / duration * 100:.0f}% 的音频")
    log(
        f"[转写] 完成：{len(text)} 字，总耗时 {time.perf_counter() - started:.1f}s"
        f"（含模型加载 {model_seconds:.1f}s）"
    )

    return TranscribeResult(
        text=text,
        duration=duration,
        duration_after_vad=duration_after_vad,
        rtf=transcribe_elapsed / duration if duration else 0.0,
        model_seconds=model_seconds,
        stats=stats,
        timestamps=timestamps,
    )
