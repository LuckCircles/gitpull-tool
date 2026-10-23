"""克隆 Worker — 后台线程执行 git clone，通过 Signal 通知 UI。

线程安全规范：
    后台线程严禁直接操作任何 Qt 控件。
    所有 UI 更新通过以下信号发出，由 UI 线程的 Slot 接收处理：
        - progress(str):  阶段描述（如 "正在克隆（克隆源 1/3）..."）
        - output(str):     git 实时输出行
        - finished(bool, str): 克隆完成（success, message）

架构层级：
    UI (CloneRepoDialog)
      └── workers.clone_worker.CloneWorker (QObject + QThread)
            └── run_clone_candidates() 纯函数（多候选源依次尝试）
                  └── subprocess.Popen (实时读取 stdout)
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys

from loguru import logger
from PySide6.QtCore import QObject, Signal

from app.common.subprocess_utils import build_hidden_subprocess_kwargs, run_hidden

# 匹配 run_clone_candidates 输出的尝试行，用于驱动 progress 阶段文本
_ATTEMPT_LINE_RE = re.compile(r"^> 尝试克隆源 (\d+)/(\d+):")


def _remove_tree(path: str) -> None:
    """删除半成品克隆目录（Windows 下 git 对象为只读，需 chmod 后重试）。"""

    def _onexc(func, p, _exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_onexc)


def _terminate_process(proc: subprocess.Popen) -> None:
    """强制终止克隆子进程（Windows 用 taskkill /T 连带子进程）。"""
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
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        logger.debug(f"终止克隆进程失败: {str(e)}")


def run_clone_candidates(
    candidates: list[str],
    base_dir: str,
    repo_name: str,
    log=print,
    cancel_check=lambda: False,
    process_cb=None,
) -> tuple[bool, str]:
    """依次尝试候选克隆源，任一成功即返回（模块级纯函数，无 Qt 依赖）。

    Args:
        candidates:  克隆候选 URL 列表（主源在前）
        base_dir:    克隆目标父目录（git clone 的 cwd）
        repo_name:   目标仓库名（目录名）
        log:         日志回调（接收每行输出）
        cancel_check: 取消检查回调，返回 True 时中止
        process_cb:  每次 Popen 后回调（供调用方跟踪进程以支持取消）

    Returns:
        (success, message)
    """
    target_path = os.path.abspath(os.path.join(base_dir, repo_name))

    # 目标父目录可能尚未创建（用户填了新路径）：先确保存在，
    # 否则后续 Popen(cwd=base_dir) 直接 FileNotFoundError。
    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError as e:
        return False, f"无法创建克隆目录 {base_dir}: {e}"

    last_code: int | None = None

    if not candidates:
        return False, "没有可用的克隆源"

    for idx, url in enumerate(candidates, start=1):
        if cancel_check():
            return False, "克隆已取消"

        log(f"> 尝试克隆源 {idx}/{len(candidates)}: {url}")

        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["GIT_TERMINAL_PROMPT"] = "0"

        proc = subprocess.Popen(
            ["git", "clone", "--progress", url, repo_name],
            cwd=base_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **build_hidden_subprocess_kwargs(),
        )
        if process_cb is not None:
            process_cb(proc)

        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_check():
                break
            line_text = line.rstrip()
            if line_text:
                log(line_text)

        if cancel_check():
            _terminate_process(proc)
            proc.wait()
            if os.path.exists(target_path):
                try:
                    _remove_tree(target_path)
                except OSError as e:
                    logger.warning(f"取消后清理半成品目录失败: {e}")
            return False, "克隆已取消"

        proc.wait()
        code = proc.returncode
        last_code = code

        if code == 0:
            return True, f"仓库 {repo_name} 克隆成功"

        # 该候选失败：清理半成品目录后尝试下一个
        if os.path.exists(target_path):
            try:
                _remove_tree(target_path)
            except OSError as e:
                logger.warning(f"清理克隆失败残留目录失败: {e}")

    return False, f"克隆失败：已尝试 {len(candidates)} 个克隆源（最后退出码 {last_code}）"


class CloneWorker(QObject):
    """后台执行 git clone 的 Worker（多候选源依次尝试），通过信号通知 UI。"""

    progress = Signal(str)  # 阶段描述
    output = Signal(str)  # git 实时输出行
    finished = Signal(bool, str)  # success, message

    def __init__(self, candidates: list[str], base_dir: str, repo_name: str):
        super().__init__()
        self._candidates = list(candidates)
        self._base_dir = base_dir
        self._repo_name = repo_name
        # 公开属性，供 UI 层在 finished 后读取目标路径
        self.target_path = os.path.abspath(os.path.join(base_dir, repo_name))
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def run(self):
        """在后台线程中执行 git clone，实时输出日志。"""
        try:
            if os.path.exists(self.target_path):
                self.finished.emit(False, f"目录已存在: {self._repo_name}")
                return

            total = len(self._candidates)
            self.progress.emit(f"正在克隆（共 {total} 个克隆源）...")
            self.output.emit(f"> git clone --progress（候选源共 {total} 个）")
            self.output.emit(f"  目标目录: {self.target_path}")
            self.output.emit("")

            success, message = run_clone_candidates(
                self._candidates,
                self._base_dir,
                self._repo_name,
                log=self._emit_output,
                cancel_check=lambda: self._cancelled,
                process_cb=self._track_process,
            )
            self._process = None
            self.finished.emit(success, message)

        except FileNotFoundError:
            self.finished.emit(
                False, "未找到 git 命令，请确认已安装 Git 并添加到系统 PATH"
            )
        except Exception as e:
            logger.error(f"克隆异常: {str(e)}")
            self.finished.emit(False, f"克隆异常: {str(e)}")
        finally:
            self._process = None

    def _emit_output(self, line: str) -> None:
        """log 回调：转发输出行；尝试行同时驱动 progress 阶段文本。"""
        m = _ATTEMPT_LINE_RE.match(line)
        if m:
            self.progress.emit(f"正在克隆（克隆源 {m.group(1)}/{m.group(2)}）...")
        self.output.emit(line)

    def _track_process(self, proc: subprocess.Popen) -> None:
        """process_cb：跟踪当前子进程以支持取消。"""
        self._process = proc

    def cancel(self):
        """请求取消克隆并终止子进程。"""
        self._cancelled = True
        if self._process and self._process.poll() is None:
            _terminate_process(self._process)
