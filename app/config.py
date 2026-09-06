"""应用配置模块 — 路径常量、配置读写、仓库缓存、日志初始化。"""

from __future__ import annotations

import json
import platform
import sys
import threading
from pathlib import Path

from loguru import logger

logger.remove()


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
def get_app_data_dir() -> Path:
    try:
        exe_path = Path(sys.argv[0]).resolve()
        return exe_path.parent
    except Exception:
        return Path.cwd()


APP_DATA_DIR = get_app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
REPO_CACHE_FILE = APP_DATA_DIR / "repo_cache.json"

# 启动时清理旧日志：删除主日志及所有相关文件（轮转产物、.bak 备份等），
# 每次运行从空日志开始
for _old in APP_DATA_DIR.glob("git_manager.log*"):
    try:
        _old.unlink()
    except OSError:
        pass

logger.add(
    str(APP_DATA_DIR / "git_manager.log"),
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    encoding="utf-8",
    enqueue=True,  # 队列写入：多线程安全，不阻塞 UI 线程
    backtrace=True,  # 异常时输出完整调用链
    diagnose=True,  # 异常栈附带变量值，便于定位问题
    catch=True,  # sink 自身异常不打断应用
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
)

# 从终端启动时同步输出到控制台（打包为窗口程序后无 stderr 则自动跳过）
if sys.stderr is not None:
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    )


# ---------------------------------------------------------------------------
# 全局异常捕获 → 日志（避免异常只打印到 stderr 后丢失）
# ---------------------------------------------------------------------------
def _excepthook(exc_type, exc_value, exc_tb):
    logger.opt(exception=(exc_type, exc_value, exc_tb)).error("未捕获的异常")


def _thread_excepthook(args: threading.ExceptHookArgs):
    logger.opt(
        exception=(args.exc_type, args.exc_value, args.exc_traceback)
    ).error(f"线程 {args.thread.name if args.thread else '?'} 未捕获的异常")


sys.excepthook = _excepthook
threading.excepthook = _thread_excepthook

logger.info(
    f"运行环境: Python {platform.python_version()} | {platform.platform()} | "
    f"程序目录: {APP_DATA_DIR}"
)


# ---------------------------------------------------------------------------
# 仓库缓存
# ---------------------------------------------------------------------------
def load_repo_cache() -> list[dict]:
    try:
        if not REPO_CACHE_FILE.exists():
            return []
        with REPO_CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_repo_cache(data: list[dict]):
    try:
        with REPO_CACHE_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 应用配置
# ---------------------------------------------------------------------------
def load_config() -> dict:
    try:
        if not CONFIG_FILE.exists():
            return {}
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载配置失败: {str(e)}")
        return {}


def save_config(config: dict):
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存配置失败: {str(e)}")
