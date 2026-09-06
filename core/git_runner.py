"""Git 命令执行器 — UI 层统一通过此类与 git 交互。

架构层级：
    UI (GitManager / Dialogs)
      └── core.git_runner.GitRunner
            └── utils.subprocess_utils (run_hidden / build_hidden_subprocess_kwargs)
                  └── subprocess
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from loguru import logger

from utils.subprocess_utils import build_hidden_subprocess_kwargs, run_hidden

# 调试日志中 stderr 摘录的最大长度（避免刷屏）
_STDERR_SNIPPET = 400


class GitRunner:
    """Git 命令执行与进程生命周期管理。

    - run_command() / run_git(): 带进程跟踪的阻塞式执行，返回 (stdout, stderr, returncode)。
    - run_simple(): 简单一次性命令（如 git config），返回 CompletedProcess。
    - set_closing() / terminate_active_processes(): 窗口关闭时安全终止。
    """

    def __init__(self):
        self._is_closing: bool = False
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def set_closing(self):
        """标记关闭中，后续 run_command 将直接返回。"""
        self._is_closing = True

    # ------------------------------------------------------------------
    # 带进程跟踪的阻塞式执行
    # ------------------------------------------------------------------
    def run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout=60,
        env: dict | None = None,
    ):
        if self._is_closing:
            return "", "Application is closing", -1

        started = time.perf_counter()
        try:
            process_env = os.environ.copy()
            process_env.setdefault("PYTHONIOENCODING", "utf-8")
            if env:
                process_env.update(env)
            popen_kwargs = build_hidden_subprocess_kwargs()

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
            self._register_process(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process(proc)
                logger.error(f"命令执行超时: {' '.join(cmd)}")
                return "", "Command timed out", -1
            finally:
                self._unregister_process(proc)

            self._log_command(cmd, cwd, proc.returncode, started, stderr)
            return (stdout or "").strip(), (stderr or "").strip(), proc.returncode

        except Exception as e:
            logger.error(f"命令执行失败: {' '.join(cmd)}")
            logger.opt(exception=e).debug("命令执行异常栈")
            return "", str(e), -1

    def run_git(self, path: str, args: list, timeout=60):
        """便捷方法：在指定仓库路径执行 git 命令。"""
        return self.run_command(["git"] + args, cwd=path, timeout=timeout)

    # ------------------------------------------------------------------
    # 简单一次性命令（不跟踪进程，适用于 git config 等快速命令）
    # ------------------------------------------------------------------
    @staticmethod
    def run_simple(cmd, **kwargs):
        """执行一次性命令并记录调试日志（返回 CompletedProcess）。"""
        started = time.perf_counter()
        result = run_hidden(cmd, **kwargs)
        GitRunner._log_command(
            list(cmd),
            kwargs.get("cwd"),
            result.returncode,
            started,
            result.stderr if hasattr(result, "stderr") else None,
        )
        return result

    # ------------------------------------------------------------------
    # 进程管理（内部）
    # ------------------------------------------------------------------
    @staticmethod
    def _log_command(
        cmd: list[str],
        cwd: str | None,
        returncode: int | None,
        started: float,
        stderr: str | None,
    ):
        """记录一次命令执行的调试日志（命令、目录、返回码、耗时）。"""
        elapsed = time.perf_counter() - started
        text = " ".join(str(c) for c in cmd)
        where = f" @ {cwd}" if cwd else ""
        logger.debug(f"[cmd] rc={returncode} {elapsed:.2f}s{where}: {text}")
        if returncode not in (0, None) and stderr:
            snippet = str(stderr).strip().replace("\n", " | ")[:_STDERR_SNIPPET]
            if snippet:
                logger.debug(f"[cmd] stderr: {snippet}")

    def _register_process(self, proc: subprocess.Popen):
        with self._process_lock:
            self._active_processes.add(proc)

    def _unregister_process(self, proc: subprocess.Popen):
        with self._process_lock:
            self._active_processes.discard(proc)

    def _terminate_process(self, proc: subprocess.Popen):
        if proc.poll() is not None:
            return

        try:
            if sys.platform == "win32":
                run_hidden(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
        except Exception as e:
            logger.debug(f"结束进程失败 PID={proc.pid}: {str(e)}")

    def terminate_active_processes(self):
        """终止所有正在运行的子进程。"""
        with self._process_lock:
            processes = list(self._active_processes)

        for proc in processes:
            self._terminate_process(proc)
