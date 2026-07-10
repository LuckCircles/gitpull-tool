"""Filesystem operations for repositories, independent from Qt widgets."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable

from utils.subprocess_utils import rmtree


class RepoService:
    """Validate and perform repository directory operations safely."""

    @staticmethod
    def remove_repo_dir(
        base_path: str, repo_name: str, onerror: Callable | None = None
    ) -> dict:
        result = {"success": False, "error": None}
        if not repo_name or not repo_name.strip():
            result["error"] = "repo_name 不能为空"
            return result
        if os.path.sep in repo_name or (os.path.altsep and os.path.altsep in repo_name):
            result["error"] = "repo_name 不能包含路径分隔符"
            return result
        if repo_name in (".", "..") or ".." in repo_name:
            result["error"] = "repo_name 包含非法路径片段"
            return result

        base_abs = os.path.abspath(base_path)
        target_abs = os.path.abspath(os.path.join(base_abs, repo_name))
        if not os.path.exists(base_abs):
            result["error"] = "base_path 不存在"
            return result
        if not os.path.exists(target_abs):
            result["error"] = "target_path 不存在"
            return result
        if base_abs == target_abs:
            result["error"] = "禁止删除 base_path 本身"
            return result
        if not target_abs.startswith(base_abs.rstrip("\\\\/") + os.sep):
            result["error"] = "target_path 不在 base_path 子目录中"
            return result

        try:
            rmtree(target_abs, onerror=onerror)
            result["success"] = True
        except Exception as exc:
            result["error"] = str(exc)
        return result

    @staticmethod
    def remove_git_metadata(repo_path: str, onerror: Callable | None = None) -> dict:
        result = {"success": False, "error": None}
        repo_abs = os.path.abspath(repo_path)
        if not os.path.isdir(repo_abs):
            result["error"] = "仓库路径不存在"
            return result

        git_dir = os.path.join(repo_abs, ".git")
        if not os.path.exists(git_dir):
            result["error"] = ".git 文件夹不存在"
            return result

        try:
            rmtree(git_dir, onerror=onerror)
            result["success"] = True
        except Exception as exc:
            result["error"] = str(exc)
        return result

    @staticmethod
    def make_writable_and_retry(func, path, exc):
        actual_exc = exc[1] if isinstance(exc, tuple) else exc
        if isinstance(actual_exc, OSError):
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        raise actual_exc

    @staticmethod
    def is_direct_child(base_path: str, repo_path: str) -> bool:
        repo_abs = os.path.abspath(repo_path)
        repo_name = os.path.basename(repo_abs.rstrip("\\\\/"))
        expected = os.path.abspath(os.path.join(os.path.abspath(base_path), repo_name))
        return repo_abs == expected

    @classmethod
    def rename_repo(cls, base_path: str, repo_path: str, new_name: str) -> dict:
        result = {"success": False, "path": "", "error": None}
        repo_abs = os.path.abspath(repo_path)
        if not cls.is_direct_child(base_path, repo_abs):
            result["error"] = "仓库不在当前基础目录的直属子目录中"
            return result

        new_path = os.path.abspath(os.path.join(base_path, new_name))
        if os.path.exists(new_path):
            result["error"] = f"名称 '{new_name}' 已存在"
            return result

        try:
            os.rename(repo_abs, new_path)
            result.update(success=True, path=new_path)
        except Exception as exc:
            result["error"] = str(exc)
        return result
