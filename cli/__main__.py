# -*- coding: utf-8 -*-
"""让 `python -m cli` 能直接跑。

没有这个文件的话，只有 `python -m cli.main` 能跑，多打四个字。
（`python -m cli` 找的是包里的 __main__.py，不是 main.py。）
"""
import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
