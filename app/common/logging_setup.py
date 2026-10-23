# -*- coding: utf-8 -*-
"""显式日志初始化 — 收纳原 app/config.py 的 import 期副作用。

原实现把 logger.remove()/logger.add()/excepthook 赋值/旧日志清理放在
config.py 模块导入期，导致任何 import app.common.config 的代码（含测试、工具脚本）
都会触发全局日志重置与异常钩子劫持。现改为入口显式调用 init_logging()。

调用约定：必须由 main.py 的 __main__ 在 qInstallMessageHandler 之前调用一次。
幂等：重复调用安全（第二次直接返回，不重复 add sink、不重复删日志文件）。
"""

from __future__ import annotations

import platform
import sys
import threading

from loguru import logger
from PySide6.QtCore import QtMsgType

from app.common.paths import ensure_app_data_dir, migrate_legacy_data_dir

_initialized = False

# 与原 config.py 完全一致的格式串
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
)
_STDERR_FORMAT = (
    "{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
)


# ---------------------------------------------------------------------------
# 全局异常捕获 → 日志（避免异常只打印到 stderr 后丢失）
# ---------------------------------------------------------------------------
def _excepthook(exc_type, exc_value, exc_tb):
    logger.opt(exception=(exc_type, exc_value, exc_tb)).error("未捕获的异常")


def _thread_excepthook(args: threading.ExceptHookArgs):
    logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).error(
        f"线程 {args.thread.name if args.thread else '?'} 未捕获的异常"
    )


def init_logging() -> None:
    """显式初始化日志 + 旧数据迁移。必须由入口在 qInstallMessageHandler 前调用。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    logger.remove()

    data_dir = ensure_app_data_dir()
    migrate_legacy_data_dir(data_dir)

    # 清理旧日志：仅删除主日志及其衍生文件（轮转产物、.bak 备份等），
    # 每次运行从空日志开始。
    # 注意：config.json / secrets.json 等配置文件不受影响，跨次启动保留。
    for old in data_dir.glob("git_manager.log*"):
        try:
            old.unlink()
        except OSError:
            pass

    logger.add(
        str(data_dir / "git_manager.log"),
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,  # 队列写入：多线程安全，不阻塞 UI 线程
        backtrace=True,  # 异常时输出完整调用链
        diagnose=False,  # 关闭变量值 dump：避免异常栈把局部变量（可能含 token）写入日志
        catch=True,  # sink 自身异常不打断应用
        format=_FILE_FORMAT,
    )

    # 从终端启动时同步输出到控制台（打包为窗口程序后无 stderr 则自动跳过）
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=_STDERR_FORMAT,
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    logger.info(
        f"运行环境: Python {platform.python_version()} | {platform.platform()} | "
        f"程序目录: {data_dir}"
    )


# Qt 消息类型 → 日志级别（捕获 Qt 内部警告/错误，便于排查渲染与信号问题）
_QT_LOG_LEVELS = {
    QtMsgType.QtDebugMsg: "DEBUG",
    QtMsgType.QtInfoMsg: "INFO",
    QtMsgType.QtWarningMsg: "WARNING",
    QtMsgType.QtCriticalMsg: "ERROR",
    QtMsgType.QtFatalMsg: "ERROR",
}


def qt_message_handler(msg_type, context, message):
    """将 Qt 内部消息路由到日志文件（含源码位置）。"""
    level = _QT_LOG_LEVELS.get(msg_type, "DEBUG")
    where = f" ({context.file}:{context.line})" if context.file else ""
    logger.log(level, f"[Qt] {message}{where}")
