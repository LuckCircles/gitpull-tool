"""应用配置模块 — 路径常量、配置读写、仓库缓存。

注意：日志初始化（sink 配置、旧日志清理、全局异常钩子）已移至
common/logging_setup.py 的 init_logging()，由 main.py 入口显式调用，
本模块导入无副作用。
"""

from __future__ import annotations

import json

from loguru import logger

from app.common.paths import get_app_data_dir

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
APP_DATA_DIR = get_app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
REPO_CACHE_FILE = APP_DATA_DIR / "repo_cache.json"


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
