"""克隆 Worker — 后台线程执行 git clone，通过 Signal 通知 UI。

线程安全规范：
    后台线程严禁直接操作任何 Qt 控件。
    所有 UI 更新通过以下信号发出，由 UI 线程的 Slot 接收处理：
        - progress(str):  阶段描述（如 "正在克隆..."）
        - output(str):     git 实时输出行
        - finished(bool, str): 克隆完成（success, message）

架构层级：
    UI (CloneRepoDialog)
      └── workers.clone_worker.CloneWorker (QObject + QThread)
            └── subprocess.Popen (实时读取 stdout)
"""

from __future__ import annotations

import os
import subprocess
import sys

from loguru import logger
from PySide6.QtCore import QObject, Signal

from utils.subprocess_utils import build_hidden_subprocess_kwargs, run_hidden


class CloneWorker(QObject):
    """后台执行 git clone 的 Worker，通过信号通知 UI。"""

    progress = Signal(str)  # 阶段描述
    output = Signal(str)  # git 实时输出行
    finished = Signal(bool, str)  # success, message

    def __init__(self, url: str, base_dir: str, repo_name: str):
        super().__init__()
        self._url = url
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

            self.progress.emit("正在克隆...")
            self.output.emit(f"> git clone --progress {self._url}")
            self.output.emit(f"  目标目录: {self.target_path}")
            self.output.emit("")

            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env["GIT_TERMINAL_PROMPT"] = "0"

            popen_kwargs = build_hidden_subprocess_kwargs()
            self._process = subprocess.Popen(
                ["git", "clone", "--progress", self._url, self._repo_name],
                cwd=self._base_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )

            assert self._process.stdout is not None
            for line in self._process.stdout:
                if self._cancelled:
                    break
                line_text = line.rstrip()
                if line_text:
                    self.output.emit(line_text)

            self._process.wait()
            code = self._process.returncode

            if self._cancelled:
                self.finished.emit(False, "克隆已取消")
            elif code == 0:
                self.finished.emit(True, f"仓库 {self._repo_name} 克隆成功")
            else:
                self.finished.emit(False, f"克隆失败 (退出码: {code})")

        except FileNotFoundError:
            self.finished.emit(
                False, "未找到 git 命令，请确认已安装 Git 并添加到系统 PATH"
            )
        except Exception as e:
            logger.error(f"克隆异常: {str(e)}")
            self.finished.emit(False, f"克隆异常: {str(e)}")
        finally:
            self._process = None

    def cancel(self):
        """请求取消克隆并终止子进程。"""
        self._cancelled = True
        if self._process and self._process.poll() is None:
            try:
                if sys.platform == "win32":
                    run_hidden(
                        ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            except Exception as e:
                logger.debug(f"取消克隆进程失败: {str(e)}")
