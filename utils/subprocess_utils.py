"""子进程工具模块 — 隐藏窗口执行、兼容性 rmtree。"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# rmtree: Python 3.12+ 使用 onexc，旧版本使用 onerror
# ---------------------------------------------------------------------------
if sys.version_info >= (3, 12):

    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        return shutil.rmtree(path, ignore_errors=ignore_errors, onexc=onexc if onexc is not None else onerror)

else:

    @functools.wraps(shutil.rmtree)
    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        handler = onexc if onexc is not None else onerror
        kwargs = {"ignore_errors": ignore_errors}
        if handler is not None:
            kwargs["onerror"] = handler
        return shutil.rmtree(path, **kwargs)


# ---------------------------------------------------------------------------
# Windows 隐藏控制台窗口的子进程参数
# ---------------------------------------------------------------------------
def build_hidden_subprocess_kwargs() -> dict:
    kwargs = {}
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        kwargs["startupinfo"] = startupinfo
    return kwargs


def run_hidden(cmd, **kwargs):
    hidden_kwargs = build_hidden_subprocess_kwargs()
    for key, value in hidden_kwargs.items():
        kwargs.setdefault(key, value)
    return subprocess.run(cmd, **kwargs)
