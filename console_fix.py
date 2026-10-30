# -*- coding: utf-8 -*-
"""
Windows 控制台中文输出编码自适应（乱码修复）
============================================

问题根源
--------
本项目的 .py 源文件均以 UTF-8 保存，Python 3 在 Windows 上把 stdout
的 encoding 报告为 utf-8，于是 print 中文时写出 UTF-8 字节。但 Windows
中文系统控制台代码页默认是 936(GBK)，PowerShell 管道 / 部分IDE 输出面板
按 GBK 解读这些字节，UTF-8 字节被当 GBK 读 -> 乱码
（例："黄浦区" -> "榛勬郸鍖?"）。

修复思路
--------
读取控制台【实际】输出代码页（GetConsoleOutputCP），把被重定向的
stdout / stderr 重新包装为该编码：
  * 普通 PowerShell(936) -> cp936(GBK) 输出，PowerShell 按 GBK 读，正常
  * VSCode 集成终端(conpty, 65001) -> utf-8 输出，终端按 UTF-8 读，正常
  * 用户手动 chcp 65001 -> utf-8 输出，正常
  * 交互式终端(isatty) -> 跳过：Python 3.7+ 走 WriteConsoleW(Unicode API)
    本就不乱码，避免干扰
  * 无控制台(代码页=0) -> 回退到 locale 首选编码

只需在脚本入口 `import console_fix` 一次即可，无其它副作用。
"""
import sys
import io
import os


def _fix():
    if os.name != 'nt':
        return
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp and cp > 0:
            enc = f'cp{cp}'
        else:
            enc = None
        if not enc:
            try:
                import locale
                enc = locale.getpreferredencoding(False)
            except Exception:
                enc = 'utf-8'
        for name in ('stdout', 'stderr'):
            s = getattr(sys, name, None)
            if s is None:
                continue
            if s.isatty():
                continue
            buf = getattr(s, 'buffer', None)
            if buf is None:
                continue
            try:
                wrapper = io.TextIOWrapper(
                    buf, encoding=enc, errors='replace', line_buffering=True)
                setattr(sys, name, wrapper)
            except Exception:
                pass
    except Exception:
        pass


_fix()