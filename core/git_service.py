"""Application-facing Git operations.

The service owns the process runner so UI classes do not manage Git process
lifecycle or invoke global Git configuration commands directly.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from core.git_runner import GitRunner


@dataclass(frozen=True)
class RepoStatus:
    repo_path: str
    branch: str
    local_commit: str
    remote_commit: str
    status: str
    ahead_behind: str
    remote_url: str
    need_update: bool
    ignored: bool


@dataclass(frozen=True)
class GitOperationResult:
    success: bool
    error: str = ""
    already_active: bool = False


class GitService:
    """Facade for Git commands and their application lifecycle."""

    def __init__(self, runner: GitRunner | None = None):
        self._runner = runner or GitRunner()
        # per-repo 互斥锁：同一仓库的 git 写操作（fetch/pull）串行化，
        # 避免扫描与更新并发执行时产生 .git/index.lock 冲突
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()

    def _get_repo_lock(self, repo_path: str) -> threading.Lock:
        key = os.path.abspath(repo_path).lower()  # Windows 路径大小写不敏感
        with self._repo_locks_guard:
            return self._repo_locks.setdefault(key, threading.Lock())

    @contextmanager
    def repo_lock(self, repo_path: str):
        """以互斥方式对同一仓库执行 git 写操作。

        用法::

            with self._git_service.repo_lock(repo):
                self._git_service.run_git(repo, ["pull", "--rebase"])

        注意：锁不可重入，持锁期间不要再获取同一仓库的锁。
        """
        with self._get_repo_lock(repo_path):
            yield

    def configure_global_quotepath(self):
        return GitRunner.run_simple(
            ["git", "config", "--global", "core.quotepath", "false"], check=False
        )

    def set_global_proxy(self, proxy: str):
        return GitRunner.run_simple(
            ["git", "config", "--global", "http.proxy", proxy], check=False
        )

    def clear_global_proxy(self):
        return GitRunner.run_simple(
            ["git", "config", "--global", "--unset", "http.proxy"], check=False
        )

    def run_git(self, path: str, args: list[str], timeout: int = 60):
        return self._runner.run_git(path, args, timeout=timeout)

    def run_command(
        self,
        command: list[str],
        cwd: str | None = None,
        timeout: int = 60,
        env: dict | None = None,
    ):
        return self._runner.run_command(command, cwd=cwd, timeout=timeout, env=env)

    def inspect_repository(
        self, repo_path: str, *, ignored: bool = False
    ) -> RepoStatus:
        """Read the status displayed for a repository during a scan."""
        repo_abs = os.path.abspath(repo_path)
        local_commit, _, _ = self.run_git(repo_abs, ["rev-parse", "--short", "HEAD"])
        branch, _, _ = self.run_git(repo_abs, ["branch", "--show-current"])
        remote_url, _, _ = self.run_git(repo_abs, ["remote", "get-url", "origin"])
        if not branch:
            branch = "游离 HEAD"

        if ignored:
            return RepoStatus(
                repo_abs,
                branch,
                local_commit or "N/A",
                "N/A",
                "⏸ 已忽略更新",
                "-",
                remote_url or "",
                False,
                True,
            )

        # fetch 持仓库锁，与更新(pull)串行化，避免并发写 .git 产生 lock 冲突
        with self.repo_lock(repo_abs):
            self.run_git(repo_abs, ["fetch", "--quiet"])
        ahead, _, _ = self.run_git(repo_abs, ["rev-list", "--count", "HEAD", "^@{u}"])
        behind, _, _ = self.run_git(repo_abs, ["rev-list", "--count", "@{u}", "^HEAD"])
        remote_commit, _, return_code = self.run_git(
            repo_abs, ["rev-parse", "--short", "@{u}"]
        )
        ahead_count = int(ahead) if ahead.isdigit() else 0
        behind_count = int(behind) if behind.isdigit() else 0

        if return_code != 0:
            status, ahead_behind, need_update = "错误", "N/A", False
        elif ahead_count == 0 and behind_count == 0:
            status, ahead_behind, need_update = "✓ 已同步", "✓", False
        else:
            status = "可更新"
            ahead_behind = f"↑{ahead_count} ↓{behind_count}"
            need_update = True

        return RepoStatus(
            repo_abs,
            branch,
            local_commit or "N/A",
            remote_commit or "N/A",
            status,
            ahead_behind,
            remote_url or "",
            need_update,
            False,
        )

    def reset_to_commit(self, repo_path: str, commit: str) -> GitOperationResult:
        _, error, code = self.run_git(repo_path, ["reset", "--hard", commit])
        return GitOperationResult(success=code == 0, error=error)

    def switch_branch(
        self, repo_path: str, branch_name: str, *, is_remote: bool = False
    ) -> GitOperationResult:
        current_branch, _, _ = self.run_git(repo_path, ["branch", "--show-current"])
        if current_branch == branch_name:
            return GitOperationResult(success=False, already_active=True)

        if is_remote:
            local_branch = (
                branch_name.split("/", 1)[1] if "/" in branch_name else branch_name
            )
            local_match, _, _ = self.run_git(
                repo_path, ["branch", "--list", local_branch]
            )
            switch_args = (
                ["switch", local_branch]
                if local_match.strip()
                else ["switch", "--track", branch_name]
            )
        else:
            switch_args = ["switch", branch_name]

        _, error, code = self.run_git(repo_path, switch_args)
        return GitOperationResult(success=code == 0, error=error)

    def shutdown(self):
        """Prevent new commands and terminate any active Git process."""
        self._runner.set_closing()
        self._runner.terminate_active_processes()
