# -*- coding: utf-8 -*-
"""K7 的环境变量与默认值。

★ 这个模块必须在任何 `huggingface_hub` 导入之前被导入。

  `HF_ENDPOINT` / `HF_HUB_DISABLE_XET` 在 `huggingface_hub` 导入那一刻
  就被读成模块常量，之后再设没有任何作用——和 `api/config.py` 是同一个坑
  （见 docs/开发说明.md 第 152–159 行）。

  `ingest/transcribe.py` 在顶层 `from ingest import config`，就是为了
  机械地保证这个顺序，而不是靠每个调用方记得先 import 谁。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── HuggingFace 下载 ──────────────────────────────────────────
#
# 用 setdefault：环境变量已经设了就以它为准。
# CI 的 runner 在墙外，workflow 里设的是 https://huggingface.co；
# 本机和服务器走国内镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 模型缓存目录：与 deploy/scitech-kb.service 的 HF_HOME 保持一致，
# 落在仓库下的 .cache/（.gitignore 已经忽略）。
# 和 HF_ENDPOINT 不同，这个值要在下面的 WhisperModel(download_root=...) 里
# 显式再用一次，所以必须读出来。
HF_HOME = os.environ.setdefault(
    "HF_HOME", str(PROJECT_ROOT / ".cache" / "huggingface")
)

# ── 口令（只在服务不绑回环时才需要）──────────────────────────
#
# api/auth.py 对 127.0.0.1 / ::1 来的请求免口令，所以本机跑 K7
# 不需要配。要把脚本指到别的机器时，才会用到 --password 或这个变量。
# 读 .env 是为了和服务器上的配置共用一份；load_dotenv 不覆盖已有环境变量。
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover —— requirements.txt 里有 python-dotenv
    pass

SHARED_PASSWORD = os.getenv("KB_SHARED_PASSWORD", "")

# ── 转写默认值 ────────────────────────────────────────────────
#
# large-v3-turbo：large-v3 的蒸馏版，中文精度接近 large，
# 纯 CPU 上比 large-v3 快一个数量级。跑不动就 --model medium/small。
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_COMPUTE_TYPE = "int8"      # 纯 CPU 上唯一合理的默认；float32 慢一倍多
DEFAULT_BEAM_SIZE = 5
DEFAULT_LANGUAGE = "zh"
DEFAULT_CHUNK_MINUTES = 15         # 长录音按静音切成这么大的块
DEFAULT_VAD_THRESHOLD = 0.5

# ── 切片默认值 ────────────────────────────────────────────────
#
# 400 字是检索质量的经验值：一条切片太长，向量会被稀释；
# 太短（原来的 160）会把一个完整回答切成好几条，检索命中了也拼不回来。
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50                 # 相邻切片重叠约 50 字（按整句取）
MIN_CHUNK = 15                     # 低于这个长度的尾部碎片并进上一片

# ── 入库 ──────────────────────────────────────────────────────
#
# 不复用 cli/client.py 的 (3, 30)：那个读超时是给检索设计的。
# 入库这边读超时给宽一点是「防重复」的正向杠杆——读超时的语义是
# 「不知道服务端写没写」，等久一点只会减少这种不可判定情形。
POST_TIMEOUT = (5, 60)
