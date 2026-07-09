"""应用配置模块 — 路径常量、配置读写、仓库缓存、日志初始化。"""

from __future__ import annotations

import json
import sys
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

logger.add(
    str(APP_DATA_DIR / "git_manager.log"), rotation="10 MB", retention="7 days", encoding="utf-8"
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
