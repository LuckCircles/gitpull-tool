"""子进程工具模块 — 隐藏窗口执行、兼容性 rmtree。"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# rmtree: Python 3.12+ 使用 onexc，旧版本使用 onerror
#
# 第三参形态说明（回调签名为 (func, path, excinfo)）：
# - onerror（旧版，3.13 起废弃、3.14 移除）：excinfo 为 sys.exc_info() 元组；
# - onexc（3.12+ 标准）：3.12.1+ 传异常实例，3.12.0 误传元组（gh-102153 已修复）。
# 本封装统一把第三参规范化为「异常实例」，调用方 handler 无需关心版本差异。
# ---------------------------------------------------------------------------
def _as_exc_instance(exc) -> BaseException:
    """兼容元组（onerror / 3.12.0）与异常实例（3.12.1+ onexc）两种形态。"""
    if isinstance(exc, tuple) and len(exc) == 3:
        return exc[1]
    return exc


if sys.version_info >= (3, 12):

    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        handler = onexc if onexc is not None else onerror
        if handler is None:
            return shutil.rmtree(path, ignore_errors=ignore_errors)

        def _onexc(func, p, exc):
            return handler(func, p, _as_exc_instance(exc))

        return shutil.rmtree(path, ignore_errors=ignore_errors, onexc=_onexc)

else:

    @functools.wraps(shutil.rmtree)
    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        handler = onexc if onexc is not None else onerror
        kwargs = {"ignore_errors": ignore_errors}
        if handler is not None:
            if onexc is not None:
                # onexc 风格 handler 期望异常实例；旧版 onerror 回调传 exc_info 元组，
                # 此处统一解包后再转交
                def _onerror(func, p, exc):
                    return handler(func, p, _as_exc_instance(exc))

                kwargs["onerror"] = _onerror
            else:
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
