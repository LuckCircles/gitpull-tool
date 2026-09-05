"""Repository update rules, independent from UI notifications.

更新算法（按序执行）：
1. 中间状态检查与自愈：
   - 存在未完成的 merge → 拒绝更新（需要用户手动处理）
   - 存在未完成的 rebase（上次更新中断残留）→ `rebase --abort`
     自动恢复到中断前状态后继续
2. 工作区检查（区分未跟踪 / 已跟踪改动）：
   - 未跟踪文件（`??`）不阻碍 rebase，不再因此拒绝更新
   - 已跟踪文件有改动 → `--autostash` 自动暂存并在更新后恢复
3. lock 处理：
   - pull 前若存在 `.git/index.lock`：短暂等待外部操作结束；
     仍存在则视为上次异常终止的陈旧锁，清理后继续
   - pull 失败且错误涉及 lock → 清理锁文件后自动重试一次
4. 远端删库/归档预检验（pull 前执行，防损毁）：
   - fetch 失败且报 404 / Repository not found → 判定远端已删库，阻断
   - 游离 HEAD / 远端分支不存在 → 明确提示，阻断
   - 远端分支最新提交仅剩 README/ARCHIVE 等说明文档（或空分支）
     → 判定远端已删库或归档，阻断本次更新以保护本地代码
5. pull 持 per-repo 锁（见 GitService.repo_lock）与扫描 fetch 串行化；
   网络类错误（超时/断连/代理故障）退避后自动重试一次；超时 180s
6. pull 失败善后：
   - 遗留 rebase 中间状态 → 自动 `rebase --abort` 恢复到更新前状态
7. pull 成功后完整性兜底：若远端仅剩说明文档（预检验被网络问题绕过
   等场景）且本地原本无改动 → 自动 reset 回滚到更新前提交并报失败
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass

from core.git_service import GitService

# pull / fetch 超时（秒）：大仓库 rebase/autostash 较慢，
# 超时会被强杀并残留 index.lock
PULL_TIMEOUT = 180

# 等待外部 git 进程释放 lock 的最长时间（秒）
LOCK_WAIT_SECONDS = 2.0

# 网络类错误重试前的退避时间（秒）
NETWORK_RETRY_DELAY = 2.0


@dataclass(frozen=True)
class UpdateResult:
    repo_path: str
    repo_name: str
    success: bool
    message: str = ""
    attempted: bool = False
    file_warning: str = ""


class UpdateService:
    """Validate and update repositories through GitService."""

    # 删库/归档说明文档的文件主干名（不含扩展名，大小写不敏感）。
    # 远端分支仅剩 1~3 个此类文件时判定为"仅剩说明文档"。
    _ARCHIVE_DOC_STEMS = {
        "readme",
        "archive",
        "archived",
        "notice",
        "archive_notice",
        "archived_notice",
        "this_repo_is_archived",
        "this_repo_is_deleted",
        "this_repo_has_been_archived",
    }

    # 远端删库特征（404 / not found / 仓库不存在 / 无法读取远端）
    _REMOTE_GONE_PATTERN = re.compile(
        r"(404"
        r"|repo(?:sitory)?\s+\S+\s+not\s+found"
        r"|repo\s+not\s+found"
        r"|does\s+not\s+appear\s+to\s+be"
        r"|could\s+not\s+read\s+from\s+remote\s+repository)",
        re.IGNORECASE,
    )

    def __init__(self, git_service: GitService):
        self._git_service = git_service

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def update_repo(self, repo_path: str, *, ignored: bool = False) -> UpdateResult:
        repo_abs = os.path.abspath(repo_path)
        repo_name = os.path.basename(repo_abs.rstrip("\\/"))

        if ignored:
            return UpdateResult(repo_abs, repo_name, False, "该仓库已设置忽略更新")

        if os.path.exists(os.path.join(repo_abs, ".git", "MERGE_HEAD")):
            return UpdateResult(
                repo_abs,
                repo_name,
                False,
                "存在未完成的 merge，请先处理后再更新。",
            )

        # 上次更新中断残留的 rebase → 自动恢复后再继续
        self._abort_stale_rebase(repo_abs)

        # 工作区检查：只把「已跟踪文件的改动」视为需要处理的脏状态。
        # 未跟踪文件（?? 开头）不阻碍 pull --rebase。
        status_out, _, _ = self._git_service.run_git(
            repo_abs, ["status", "--porcelain"]
        )
        has_tracked_changes = any(
            line and not line.startswith("??") for line in status_out.splitlines()
        )

        files_before = self._count_tracked_files(repo_abs)
        before_sha = self._current_head(repo_abs)

        # pull 持仓库锁，与扫描线程的 fetch 串行化
        with self._git_service.repo_lock(repo_abs):
            self._wait_or_clear_stale_lock(repo_abs)

            # 远端删库/归档预检验（返回非空 = 阻断原因）
            block_reason = self._check_remote_health(repo_abs)
            if block_reason:
                return UpdateResult(repo_abs, repo_name, False, block_reason)

            output, error, code = self._pull_with_retry(
                repo_abs, autostash=has_tracked_changes
            )

            # 失败善后：恢复 rebase 中间状态（autostash 会一并恢复）
            if code != 0 and self._abort_stale_rebase(repo_abs):
                error = f"{error}\n（已自动恢复到更新前的状态）" if error else "已自动恢复到更新前的状态"

        if code == 0:
            # 兜底：预检验被网络问题绕过、远端实际只剩说明文档 →
            # 本地无改动时自动回滚，保护本地代码
            if not has_tracked_changes and before_sha:
                if self._rollback_if_archived(repo_abs, before_sha, files_before):
                    return UpdateResult(
                        repo_abs,
                        repo_name,
                        False,
                        "远端仓库疑似已删除或归档（仅剩说明文档），"
                        "已自动回滚本次更新以保护本地代码。"
                        "若确认远端已删库，可删除本地仓库或设置忽略更新。",
                        attempted=True,
                    )

            warning = self._check_repo_integrity(repo_abs, files_before)
            if has_tracked_changes:
                message = "已自动暂存本地改动并在更新后恢复"
            elif before_sha:
                after_sha = self._current_head(repo_abs)
                message = "已更新到新版本" if after_sha != before_sha else ""
            else:
                message = ""
            return UpdateResult(
                repo_abs,
                repo_name,
                True,
                message,
                attempted=True,
                file_warning=warning,
            )

        return UpdateResult(
            repo_abs,
            repo_name,
            False,
            self._format_pull_error(error, output),
            attempted=True,
        )

    # ------------------------------------------------------------------
    # 远端删库/归档检验
    # ------------------------------------------------------------------
    def _check_remote_health(self, repo_abs: str) -> str:
        """pull 前远端健康检验。

        返回非空字符串表示应阻断本次更新的原因；空字符串表示可以继续。
        - fetch 报 404 / Repository not found → 远端已删库
        - 游离 HEAD / 远端分支不存在 → 无法更新
        - 远端分支最新提交仅剩说明文档或为空 → 疑似删库/归档，阻断
        - 其它 fetch 失败（网络抖动等）不阻断，交由 pull 的重试逻辑兜底
        """
        url, _, _ = self._git_service.run_git(
            repo_abs, ["remote", "get-url", "origin"]
        )
        if not url.strip():
            return "未配置 origin 远端，无法更新。"

        branch, _, _ = self._git_service.run_git(
            repo_abs, ["branch", "--show-current"]
        )
        branch = branch.strip()
        if not branch:
            return "当前处于游离 HEAD 状态，请先切换到分支后再更新。"

        # 先 fetch（--prune 清理已删除的远端跟踪分支），pull 内部的
        # fetch 随后为增量操作，几乎无额外开销
        output, error, code = self._git_service.run_git(
            repo_abs, ["fetch", "--quiet", "--prune", "origin"],
            timeout=PULL_TIMEOUT,
        )
        if code != 0:
            if self._is_remote_gone_error(error) or self._is_remote_gone_error(
                output
            ):
                return (
                    "远端仓库已删除或不可访问（404），无法更新。"
                    "若确认远端已删库，可删除本地仓库或设置忽略更新。"
                )
            # 网络等其他错误：不阻断，pull 时还有重试机会
            return ""

        remote_ref = f"origin/{branch}"
        sha, _, code = self._git_service.run_git(
            repo_abs, ["rev-parse", "--verify", "--quiet", remote_ref]
        )
        sha = sha.strip()
        if code != 0 or not sha:
            return (
                f"远端分支 {remote_ref} 不存在（可能已被删除），已阻止本次更新。"
                "请确认远端仓库状态后再操作。"
            )

        # 检验远端分支最新提交的内容是否只剩说明文档（删库/归档特征）
        out, _, code = self._git_service.run_git(
            repo_abs, ["ls-tree", "-r", "--name-only", sha]
        )
        if code != 0:
            return ""  # 无法检验时不阻断，交由完整性兜底

        remote_files = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not remote_files:
            return (
                "远端分支已是空分支（无任何文件），疑似远端仓库已被清空，"
                "已阻止本次更新以保护本地代码。"
            )
        if self._is_archive_only(remote_files):
            names = ", ".join(
                os.path.basename(f) for f in remote_files
            )
            return (
                f"远端仓库疑似已删除或归档：远端分支仅剩说明文档（{names}），"
                "已阻止本次更新以保护本地代码。若确认需要同步，请手动处理。"
            )
        return ""

    @classmethod
    def _is_archive_only(cls, files: list[str]) -> bool:
        """文件清单是否仅由删库/归档说明文档构成（1~3 个文件）。"""
        if not (1 <= len(files) <= 3):
            return False
        stems = {
            os.path.splitext(os.path.basename(f))[0].lower() for f in files
        }
        return stems <= cls._ARCHIVE_DOC_STEMS

    @classmethod
    def _is_remote_gone_error(cls, error: str) -> bool:
        """错误信息是否表明远端仓库已删除（404 / not found / 无法读取）。"""
        if not error:
            return False
        return bool(cls._REMOTE_GONE_PATTERN.search(error))

    def _rollback_if_archived(
        self, repo_abs: str, before_sha: str, files_before: int
    ) -> bool:
        """pull 成功后的兜底检验：远端只剩说明文档时回滚到更新前提交。

        仅在本地原本无已跟踪改动（无 autostash）且原本文件较多时执行，
        避免覆盖用户的本地修改。返回是否执行了回滚。
        """
        if files_before <= 3:
            return False
        files_after = self._list_tracked_files(repo_abs)
        if not self._is_archive_only(files_after):
            return False
        self._git_service.run_git(repo_abs, ["reset", "--hard", before_sha])
        return True

    # ------------------------------------------------------------------
    # pull 执行、lock / 网络重试
    # ------------------------------------------------------------------
    def _pull_with_retry(self, repo_abs: str, *, autostash: bool):
        """执行 pull：lock 冲突清锁重试，网络错误退避后重试一次。"""
        args = ["-c", "core.editor=true", "pull", "--rebase"]
        if autostash:
            args.append("--autostash")

        output, error, code = self._pull_once(repo_abs, args)
        if code == 0:
            return output, error, code

        # 网络类错误（超时/断连/代理故障）→ 退避后重试一次
        if self._is_network_error(error, output):
            time.sleep(NETWORK_RETRY_DELAY)
            return self._pull_once(repo_abs, args)
        return output, error, code

    def _pull_once(self, repo_abs: str, args: list[str]):
        """单次 pull，遇到 lock 冲突时清理锁文件并重试一次。"""
        output, error, code = self._git_service.run_git(
            repo_abs, args, timeout=PULL_TIMEOUT
        )
        if code == 0 or not self._is_lock_error(error):
            return output, error, code

        # lock 冲突（多为外部 git 进程瞬时占用或上次残留）→ 清理后重试
        self._clear_locks(repo_abs)
        return self._git_service.run_git(repo_abs, args, timeout=PULL_TIMEOUT)

    @staticmethod
    def _is_network_error(error: str, output: str = "") -> bool:
        """错误信息是否为网络类故障（值得退避重试）。"""
        low = (error or "") + "\n" + (output or "")
        low = low.lower()
        markers = (
            "timed out",
            "timeout",
            "could not resolve host",
            "name or service not known",
            "failed to connect",
            "connection refused",
            "connection was reset",
            "connection was closed",
            "ssl",
            "proxy",
            "502",
            "503",
            "504",
            "rpc failed",
            "early eof",
            "empty reply from server",
            "unable to access",
        )
        return any(m in low for m in markers)

    @staticmethod
    def _is_lock_error(error: str) -> bool:
        if not error:
            return False
        markers = ("index.lock", "shallow.lock", "Another git process")
        return any(m in error for m in markers)

    def _wait_or_clear_stale_lock(self, repo_abs: str):
        """pull 前处理已存在的 index.lock。

        有锁说明可能有外部 git 进程（IDE/终端）正在操作，先短暂等待；
        等不到则视为上次异常终止的陈旧锁（本进程内的并发已由 per-repo
        锁串行化，不会产生锁），删除后继续。
        """
        lock = os.path.join(repo_abs, ".git", "index.lock")
        if not os.path.exists(lock):
            return

        deadline = time.time() + LOCK_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(0.25)
            if not os.path.exists(lock):
                return

        try:
            os.remove(lock)
        except OSError:
            pass  # 删除失败（被占用等），交由 pull 失败后的重试兜底

    @staticmethod
    def _clear_locks(repo_abs: str):
        """清理仓库常见的 git 锁文件（仅 pull 失败重试时调用）。"""
        git_dir = os.path.join(repo_abs, ".git")
        if not os.path.isdir(git_dir):
            return
        for name in ("index.lock", "shallow.lock"):
            path = os.path.join(git_dir, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # rebase 中间状态自愈
    # ------------------------------------------------------------------
    def _abort_stale_rebase(self, repo_abs: str) -> bool:
        """若仓库处于未完成的 rebase 状态，自动恢复。

        `rebase --abort` 会恢复到 rebase 开始前的提交；若该 rebase 带
        autostash，暂存的本地改动也会自动恢复。abort 失败（状态损坏）
        时降级为 `rebase --quit` / 强制清理目录，避免仓库被卡死。
        返回是否执行了恢复。
        """
        git_dir = os.path.join(repo_abs, ".git")
        paths = [os.path.join(git_dir, name) for name in ("rebase-merge", "rebase-apply")]
        if not any(os.path.exists(p) for p in paths):
            return False

        self._git_service.run_git(repo_abs, ["rebase", "--abort"])
        if not any(os.path.exists(p) for p in paths):
            return True

        # abort 失败（目录残留）→ 放弃 rebase 记账后强制清理
        self._git_service.run_git(repo_abs, ["rebase", "--quit"])
        for p in paths:
            shutil.rmtree(p, ignore_errors=True)
        return True

    # ------------------------------------------------------------------
    # 完整性检查
    # ------------------------------------------------------------------
    def _count_tracked_files(self, repo_abs: str) -> int:
        """统计 git 已跟踪文件数（读 index，不受未跟踪目录如 node_modules 干扰）。"""
        return len(self._list_tracked_files(repo_abs))

    def _list_tracked_files(self, repo_abs: str) -> list[str]:
        out, _, code = self._git_service.run_git(repo_abs, ["ls-files"])
        if code != 0:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def _check_repo_integrity(self, repo_abs: str, files_before: int) -> str:
        """更新后完整性检查，识别删库 / 文件清零等异常变动。"""
        if not os.path.isdir(repo_abs):
            return "仓库目录丢失，疑似被删除"

        if not os.path.exists(os.path.join(repo_abs, ".git")):
            return ".git 目录丢失，仓库 Git 信息可能被删除"

        files_after = self._list_tracked_files(repo_abs)
        if not files_after:
            if files_before > 0:
                return (
                    f"疑似文件清零：更新后无已跟踪文件"
                    f"（更新前 {files_before} 个文件）"
                )
            return "更新后无已跟踪文件"

        if self._is_archive_only(files_after):
            names = ", ".join(os.path.basename(f) for f in files_after)
            return f"仓库仅剩说明文档（{names}），疑似远端已删库或归档，请检查"

        if files_before >= 20 and len(files_after) < files_before * 0.1:
            return (
                f"文件数大幅减少：{files_before} → {len(files_after)}，请检查是否误删"
            )

        return ""

    def _current_head(self, repo_abs: str) -> str:
        out, _, code = self._git_service.run_git(
            repo_abs, ["rev-parse", "HEAD"]
        )
        return out.strip() if code == 0 else ""

    # ------------------------------------------------------------------
    # 错误信息
    # ------------------------------------------------------------------
    @staticmethod
    def _format_pull_error(error: str, output: str) -> str:
        """提取对用户最有用的 pull 失败信息（含删库识别）。"""
        text = (error or output or "").strip()
        if not text:
            return "更新过程中出现未知错误。"

        if UpdateService._is_remote_gone_error(text):
            return (
                "远端仓库已删除或不可访问（404）。"
                "若确认远端已删库，可删除本地仓库或设置忽略更新。"
            )

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        tail = " | ".join(lines[-3:]) if lines else text
        return tail[:300]
