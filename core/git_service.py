"""Application-facing Git operations.

The service owns the process runner so UI classes do not manage Git process
lifecycle or invoke global Git configuration commands directly.
"""

from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from loguru import logger

from core.git_runner import GitRunner

# 远端仓库已删除的 git 报错特征（覆盖 HTTPS 与 SSH 两种传输、GitHub/Gitee 等）：
#   HTTPS: fatal: repository 'https://...' not found
#          fatal: unable to access '...': The requested URL returned error: 404
#   SSH:   ERROR: Repository not found.
# 注意：刻意不匹配 "Could not read from remote repository"（网络断开/密钥问题
# 也会出现，会造成误判）；404 是删库/私有不可见的确切信号
_REMOTE_DELETED_PAT = re.compile(
    r"repository\s+'[^']*'\s+not\s+found"
    r"|repository\s+not\s+found"
    r"|returned\s+error:\s*404",
    re.IGNORECASE,
)


def is_remote_deleted(stderr: str) -> bool:
    """判断 fetch 的 stderr 是否表明远端仓库已被删除。"""
    return bool(stderr) and bool(_REMOTE_DELETED_PAT.search(stderr))


# Windows schannel（git 默认 TLS 后端）与代理不兼容时的握手失败特征：
#   schannel: failed to receive handshake, SSL/TLS connection failed
#   schannel: next InitializeSecurityContext failed: CRYPT_E_REVOCATION_OFFLINE
#   OpenSSL SSL_read: ... tlsv1 alert ...
# 这类失败与网络无关、重试无效，换 openssl 后端或直连即可恢复
_TLS_HANDSHAKE_PAT = re.compile(
    r"schannel:\s*(?:failed to receive handshake|next InitializeSecurityContext failed"
    r"|security package|unable to complete handshake)"
    r"|SSL/TLS connection failed"
    r"|CRYPTO_E_REVOCATION_OFFLINE"
    r"|certificate revocation (?:check|status) (?:failed|offline)",
    re.IGNORECASE,
)

_TLS_HINT = (
    "当前 git 使用 Windows schannel TLS 后端，与代理不兼容导致握手失败"
    "（重试无效）。解决方式二选一："
    "① 在设置页关闭「代理」开关，改用直连；"
    "② 改用 openssl 后端：执行 git config --global http.sslBackend openssl"
)


def tls_backend_hint(stderr: str) -> str:
    """识别 schannel TLS 后端与代理不兼容的握手失败，返回可操作提示；否则返回空串。"""
    return _TLS_HINT if stderr and _TLS_HANDSHAKE_PAT.search(stderr) else ""


# 本地领先远端的提交数达到该阈值时，判定远端历史疑似被删库重写/强制回退。
# 本工具管理的仓库均为克隆所得，正常情况下本地不应大量领先远端；
# 领先数十个提交通常意味着远端被 force-push 截断或删库重建
# （如远端只剩 README，本地仍保留完整历史）
AHEAD_DELETION_THRESHOLD = 20


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
        self, repo_path: str, *, ignored: bool = False, fetch: bool = True,
        assume_fresh: bool = False,
    ) -> RepoStatus:
        """Read the status displayed for a repository during a scan.

        fetch=False 时仅读取本地信息（分支/版本/远端地址，毫秒级、无网络），
        状态列返回 "⏳ 同步中"，供扫描两阶段加载先行展示仓库列表。
        assume_fresh=True 时跳过 fetch 直接计算最终状态 —— 适用于刚克隆
        完成的仓库（远端引用与克隆时一致，再 fetch 是纯冗余网络往返）。
        """
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

        if not fetch:
            return RepoStatus(
                repo_abs,
                branch,
                local_commit or "N/A",
                "...",
                "⏳ 同步中",
                "-",
                remote_url or "",
                False,
                False,
            )

        # fetch 持仓库锁，与更新(pull)串行化，避免并发写 .git 产生 lock 冲突；
        # --prune 同步清除远端已删除分支的跟踪引用，使 @{u} 如实反映远端状态
        # （VS Code 的默认 fetch 行为）；assume_fresh（刚克隆完成）不 fetch，也无需持锁
        if not assume_fresh:
            with self.repo_lock(repo_abs):
                _, fetch_err, fetch_rc = self.run_git(
                    repo_abs, ["fetch", "--quiet", "--prune"]
                )
            # fetch 失败时识别“远端仓库已删除”的确切特征（404 / not found）；
            # 网络超时/密钥问题等其他失败保持原行为（基于旧远端引用继续计算）
            if fetch_rc != 0:
                # schannel 与代理不兼容：状态仍按旧引用计算，但必须让用户看见，
                # 否则表现为「扫描一切正常、点更新却失败」，极难自行定位
                tls_hint = tls_backend_hint(fetch_err)
                if tls_hint:
                    logger.warning(f"[fetch] {os.path.basename(repo_abs)}: {tls_hint}")
            if fetch_rc != 0 and is_remote_deleted(fetch_err):
                return RepoStatus(
                    repo_abs,
                    branch,
                    local_commit or "N/A",
                    "N/A",
                    "远端已删除",
                    "N/A",
                    remote_url or "",
                    False,
                    False,
                )
        ahead, _, _ = self.run_git(repo_abs, ["rev-list", "--count", "HEAD", "^@{u}"])
        behind, _, _ = self.run_git(repo_abs, ["rev-list", "--count", "@{u}", "^HEAD"])
        remote_commit, _, return_code = self.run_git(
            repo_abs, ["rev-parse", "--short", "@{u}"]
        )
        ahead_count = int(ahead) if ahead.isdigit() else 0
        behind_count = int(behind) if behind.isdigit() else 0

        if return_code != 0:
            # @{u} 无法解析：若分支配置过上游（branch.X.merge 存在）但跟踪引用
            # 已被 --prune 清除，说明远端删除了该分支；从未配置上游则保持“错误”
            merge_ref, _, merge_rc = self.run_git(
                repo_abs, ["config", "--get", f"branch.{branch}.merge"]
            )
            if merge_rc == 0 and merge_ref.strip():
                status, ahead_behind, need_update = "远端分支已删除", "N/A", False
            else:
                status, ahead_behind, need_update = "错误", "N/A", False
        elif ahead_count == 0 and behind_count == 0:
            status, ahead_behind, need_update = "✓ 已同步", "✓", False
        elif ahead_count >= AHEAD_DELETION_THRESHOLD:
            # 本地大量领先远端：远端历史疑似被删库重写/强制回退
            # （远端只剩 README 等场景），按删库处理，不参与更新
            status = "疑似远端删库"
            ahead_behind = f"↑{ahead_count} ↓{behind_count}"
            need_update = False
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
