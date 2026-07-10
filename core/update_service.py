"""Repository update rules, independent from UI notifications."""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.git_service import GitService


@dataclass(frozen=True)
class UpdateResult:
    repo_path: str
    repo_name: str
    success: bool
    message: str = ""
    attempted: bool = False


class UpdateService:
    """Validate and update repositories through GitService."""

    def __init__(self, git_service: GitService):
        self._git_service = git_service

    def update_repo(self, repo_path: str, *, ignored: bool = False) -> UpdateResult:
        repo_abs = os.path.abspath(repo_path)
        repo_name = os.path.basename(repo_abs.rstrip("\\\\/"))

        if ignored:
            return UpdateResult(repo_abs, repo_name, False, "该仓库已设置忽略更新")

        if os.path.exists(os.path.join(repo_abs, ".git", "MERGE_HEAD")):
            return UpdateResult(
                repo_abs,
                repo_name,
                False,
                "存在未完成的 merge，请先处理后再更新。",
            )

        clean, _, _ = self._git_service.run_git(
            repo_abs, ["status", "--porcelain"]
        )
        if clean.strip():
            return UpdateResult(
                repo_abs,
                repo_name,
                False,
                "工作区有未提交的更改，请先提交或暂存后再更新。",
            )

        _, error, code = self._git_service.run_git(
            repo_abs, ["-c", "core.editor=true", "pull", "--rebase"]
        )
        if code == 0:
            return UpdateResult(repo_abs, repo_name, True, attempted=True)

        return UpdateResult(
            repo_abs,
            repo_name,
            False,
            error[:200] if error else "更新过程中出现未知错误。",
            attempted=True,
        )
