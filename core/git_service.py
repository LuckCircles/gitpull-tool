"""Application-facing Git operations.

The service owns the process runner so UI classes do not manage Git process
lifecycle or invoke global Git configuration commands directly.
"""

from __future__ import annotations

import os
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

    def inspect_repository(self, repo_path: str, *, ignored: bool = False) -> RepoStatus:
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

        self.run_git(repo_abs, ["fetch", "--quiet"])
        ahead, _, _ = self.run_git(
            repo_abs, ["rev-list", "--count", "HEAD", "^@{u}"]
        )
        behind, _, _ = self.run_git(
            repo_abs, ["rev-list", "--count", "@{u}", "^HEAD"]
        )
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
