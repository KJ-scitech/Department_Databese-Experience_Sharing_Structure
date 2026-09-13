"""Windows 控制台编码兜底。

Windows 的 cmd / PowerShell 默认按 GBK（cp936）输出，打印 GBK 里
没有的字符——最常见的就是 ✅ ❌ ⚠️ 这类 emoji——会直接抛
UnicodeEncodeError 把脚本打断。

这个报错特别难查：它发生在 print() 内部，堆栈里一堆 pymysql 的
调用，看起来像是数据库或代码的毛病，很难往「编码」上想。
（本脚本的初始版本就踩了：连库报错时打 ❌，结果真正的错误信息
没能打出来，屏幕上是一段 UnicodeEncodeError。）

处理办法：把 stdout / stderr 的 errors 改成 replace，
遇到编不出来的字符降级成 ?，而不是崩掉。

但**这只是兜底，不是许可证**。写脚本时仍然不要用 emoji——
降级成 ? 一样看不懂。用「完成：」「失败：」「注意：」这种
GBK 装得下的中文提示。
"""
import sys


def setup() -> None:
    """在每个脚本的 main() 开头调用一次。"""
    for stream in (sys.stdout, sys.stderr):
        # 输出被重定向到文件或管道时，拿到的可能不是 TextIOWrapper，
        # 没有 reconfigure 方法。
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
