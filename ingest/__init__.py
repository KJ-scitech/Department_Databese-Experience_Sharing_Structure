# -*- coding: utf-8 -*-
"""K7 长文进库：文件 → 文字 → 切片 → K1 接口。

━━━ 怎么跑 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    python -m ingest.run_ingest <文件路径> [选项]     ← 推荐
    python ingest/run_ingest.py <文件路径> [选项]     ← 也支持

`-m` 跑法要求当前目录是仓库根（和 `python -m scripts.xxx` 一致）。
直接跑脚本时，run_ingest.py 会把仓库根补进 sys.path——见那个文件开头。

━━━ 包里各文件干什么 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    config.py        HF 环境变量（必须在下载库导入之前）、默认参数
    extract.py       文件 → 文字（编码回退、docx/pptx/pdf/xlsx/csv）
    chunk.py         文字 → 切片（纯函数，没有模型也能单测）
    transcribe.py    音频 → 文字（模型、VAD、解码参数、速度自检）
    store.py         切片 → 入库（HTTP、去重；★ 刻意不做自动重试）
    run_ingest.py    命令行入口，把上面几块串起来

━━━ 两条必须知道的规矩 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **走 HTTP 入库，不许直接 import api.vectors / 打开 data/chroma/**
   原因和 `cli/client.py` 开头写的是同一条：同一时刻只许一个进程碰向量库。

2. **入库失败不自动重试**
   K1 没有幂等键，重试会造成重复记录。原因写在 `store.py` 开头。
"""
