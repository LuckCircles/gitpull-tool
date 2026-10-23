# -*- coding: utf-8 -*-
"""应用路径与数据目录 — 单点权威（借鉴 Fluent-M3U8 app/common/setting.py）。

frozen（Nuitka onefile）时数据目录必须与 exe 解压目录（系统缓存）解耦，
否则 config.json / secrets.json / git_manager.log 写在可能被清理的缓存目录；
开发模式保持 sys.argv[0].parent 现状（测试与本地运行零影响）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "GitPull-tool"


def is_frozen() -> bool:
    """Nuitka 编译后 __compiled__ 存在；PyInstaller 等设 sys.frozen。"""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def get_app_data_dir() -> Path:
    # ① 测试/便携覆盖优先
    override = os.environ.get("GIT_TOOL_DATA_DIR")
    if override:
        return Path(override)
    # ② 打包后：稳定用户数据目录，与 onefile 解压目录解耦
    if is_frozen():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(Path.home())
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / APP_NAME
    # ③ 开发模式：保持既有行为
    try:
        return Path(sys.argv[0]).resolve().parent
    except Exception:
        return Path.cwd()


def ensure_app_data_dir() -> Path:
    d = get_app_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_legacy_data_dir(target: Path) -> None:
    """一次性迁移：旧打包版把数据写在 exe 解压目录（sys.argv[0].parent）。
    仅 frozen 且新目录尚无该文件时复制，不覆盖新目录已有文件。"""
    if not is_frozen():
        return
    try:
        legacy = Path(sys.argv[0]).resolve().parent
    except Exception:
        return
    if legacy == target or not legacy.is_dir():
        return
    for name in ("config.json", "secrets.json", "repo_cache.json"):
        src, dst = legacy / name, target / name
        if src.is_file() and not dst.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError:
                pass
