import json
import os
import re
import shutil
import stat
import subprocess
import sys
import webbrowser

if sys.version_info >= (3, 12):

    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        return shutil.rmtree(path, ignore_errors=ignore_errors, onexc=onexc if onexc is not None else onerror)

else:
    import functools

    @functools.wraps(shutil.rmtree)
    def rmtree(path, ignore_errors=False, onerror=None, onexc=None):
        handler = onexc if onexc is not None else onerror
        kwargs = {"ignore_errors": ignore_errors}
        if handler is not None:
            kwargs["onerror"] = handler
        return shutil.rmtree(path, **kwargs)


import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QClipboard, QColor, QIcon, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import Action
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (HorizontalSeparator, IndeterminateProgressBar, InfoBar,
                            LineEdit, MessageBox, MessageBoxBase,
                            MSFluentWindow, NavigationItemPosition,
                            PrimaryPushButton, PushButton, PushSettingCard,
                            RoundMenu, SearchLineEdit, SettingCardGroup,
                            StrongBodyLabel, SubtitleLabel,
                            SwitchSettingCard, TableWidget, TextEdit, Theme,
                            ToolButton, ToolTipFilter, ToolTipPosition,
                            setTheme)


from github_url_utils import normalize_github_url

from res_rc import qInitResources

logger.remove()


def get_app_data_dir() -> Path:
    try:
        exe_path = Path(sys.argv[0]).resolve()
        return exe_path.parent
    except Exception:
        return Path.cwd()


APP_DATA_DIR = get_app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
REPO_CACHE_FILE = APP_DATA_DIR / "repo_cache.json"
logger.add(
    str(APP_DATA_DIR / "git_manager.log"), rotation="10 MB", retention="7 days", encoding="utf-8"
)


def load_repo_cache() -> list[dict]:
    try:
        if not REPO_CACHE_FILE.exists():
            return []
        with REPO_CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_repo_cache(data: list[dict]):
    try:
        with REPO_CACHE_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_config() -> dict:
    try:
        if not CONFIG_FILE.exists():
            return {}
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载配置失败: {str(e)}")
        return {}


def save_config(config: dict):
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存配置失败: {str(e)}")


def build_hidden_subprocess_kwargs() -> dict:
    kwargs = {}
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        kwargs["startupinfo"] = startupinfo
    return kwargs


def run_hidden(cmd, **kwargs):
    hidden_kwargs = build_hidden_subprocess_kwargs()
    for key, value in hidden_kwargs.items():
        kwargs.setdefault(key, value)
    return subprocess.run(cmd, **kwargs)


@dataclass
class GitRepoInfo:
    url: str


@dataclass
class GitRepoCandidate:
    name: str
    path: str
    is_git: bool
    git_path: str


def derive_repo_name(repo_input: str | GitRepoInfo) -> str:
    url = repo_input.url if isinstance(repo_input, GitRepoInfo) else str(repo_input)
    cleaned = url.strip().rstrip("/").rstrip("\\")
    if not cleaned:
        return ""

    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    repo_name = os.path.basename(cleaned.replace(":", "/"))
    return repo_name.strip()


def build_clone_candidates(repo_input: str | GitRepoInfo) -> list[str]:
    url = repo_input.url if isinstance(repo_input, GitRepoInfo) else str(repo_input)
    primary = url.strip()
    if not primary:
        return []

    candidates = [primary]
    parsed = urlparse(primary)

    if parsed.scheme in ("http", "https") and parsed.netloc.lower() == "github.com":
        candidates.append(f"https://ghproxy.com/{primary}")
    elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitee.com":
        candidates.insert(0, primary)
    elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitlab.com":
        candidates.append(primary)

    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates




def safe_remove_repo_dir(base_path: str, repo_name: str, onerror=None) -> dict:
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

    base_prefix = base_abs.rstrip("\\/") + os.sep
    if not target_abs.startswith(base_prefix):
        result["error"] = "target_path 不在 base_path 子目录中"
        return result

    try:
        rmtree(target_abs, onerror=onerror)
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def scan_git_repos(base_path: str) -> list[GitRepoCandidate]:
    if not base_path or not base_path.strip():
        return []

    if ".." in Path(base_path).parts:
        return []

    base_abs = os.path.abspath(base_path)
    if not os.path.isdir(base_abs):
        return []

    candidates: list[GitRepoCandidate] = []
    base_prefix = base_abs.rstrip("\\/") + os.sep

    try:
        for entry_name in os.listdir(base_abs):
            child_path = os.path.abspath(os.path.join(base_abs, entry_name))

            if not child_path.startswith(base_prefix):
                continue

            if not os.path.isdir(child_path):
                continue

            git_path = os.path.abspath(os.path.join(child_path, ".git"))
            if not git_path.startswith(child_path.rstrip("\\/") + os.sep):
                continue

            if os.path.exists(git_path) and os.path.isdir(git_path):
                candidates.append(
                    GitRepoCandidate(
                        name=entry_name,
                        path=child_path,
                        is_git=True,
                        git_path=git_path,
                    )
                )
    except OSError:
        return []

    return candidates


class CloneWorker(QObject):
    """后台执行 git clone 的 Worker，通过信号通知 UI。"""
    progress = Signal(str)        # 阶段描述
    output = Signal(str)          # git 实时输出行
    finished = Signal(bool, str)  # success, message

    def __init__(self, url: str, base_dir: str, repo_name: str):
        super().__init__()
        self._url = url
        self._base_dir = base_dir
        self._repo_name = repo_name
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def run(self):
        """在后台线程中执行 git clone，实时输出日志。"""
        try:
            target_path = os.path.abspath(os.path.join(self._base_dir, self._repo_name))
            if os.path.exists(target_path):
                self.finished.emit(False, f"目录已存在: {self._repo_name}")
                return

            self.progress.emit("正在克隆...")
            self.output.emit(f"> git clone --progress {self._url}")
            self.output.emit(f"  目标目录: {target_path}")
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
            self.finished.emit(False, "未找到 git 命令，请确认已安装 Git 并添加到系统 PATH")
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


class CloneRepoDialog(QDialog):
    """克隆仓库对话框 —— 支持实时输出、连续克隆、线程安全。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._worker: CloneWorker | None = None
        self._thread: QThread | None = None
        self._clone_running = False

        self.setWindowTitle("克隆仓库")
        self.resize(680, 560)

        # ---- 主布局 ----
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(SubtitleLabel("克隆仓库"))

        # ---- URL 输入行 ----
        url_row = QHBoxLayout()
        url_row.setSpacing(8)

        self.urlLineEdit = LineEdit()
        self.urlLineEdit.setPlaceholderText("输入 Git 仓库链接")
        self.urlLineEdit.setClearButtonEnabled(True)
        self.urlLineEdit.textChanged.connect(self._validate_url)
        url_row.addWidget(self.urlLineEdit, 1)

        self.paste_btn = ToolButton(FIF.PASTE)
        ToolTipFilter(self.paste_btn, showDelay=300, position=ToolTipPosition.TOP)
        self.paste_btn.setToolTip("从剪贴板粘贴")
        self.paste_btn.clicked.connect(self._paste_from_clipboard)
        url_row.addWidget(self.paste_btn)

        self.format_btn = ToolButton(FIF.CODE)
        ToolTipFilter(self.format_btn, showDelay=300, position=ToolTipPosition.TOP)
        self.format_btn.setToolTip("自动格式化链接（例如 git clone 命令 → 纯 URL）")
        self.format_btn.clicked.connect(self._format_url)
        url_row.addWidget(self.format_btn)

        layout.addLayout(url_row)

        # ---- 仓库信息区域 ----
        self._repo_name_label = StrongBodyLabel("仓库名称: -")
        layout.addWidget(self._repo_name_label)

        self._clone_dir_label = StrongBodyLabel("克隆目录: -")
        layout.addWidget(self._clone_dir_label)

        layout.addWidget(HorizontalSeparator())

        # ---- 进度区域 ----
        self._status_label = SubtitleLabel("状态: 就绪")
        layout.addWidget(self._status_label)

        self._progress_bar = IndeterminateProgressBar()
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        # ---- 实时日志窗口 ----
        layout.addWidget(StrongBodyLabel("实时日志"))
        self._log_edit = TextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setPlaceholderText("克隆开始后，Git 输出将显示在此处...")
        layout.addWidget(self._log_edit, 1)

        # ---- 按钮区域 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._start_btn = PrimaryPushButton(FIF.DOWNLOAD, "开始克隆")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_clone)
        btn_row.addWidget(self._start_btn)

        self._close_btn = PushButton(FIF.CLOSE, "关闭")
        self._close_btn.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self._close_btn)

        layout.addLayout(btn_row)

    # ---- 剪贴板粘贴 ----
    def _paste_from_clipboard(self):
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text().strip()
            if text:
                self.urlLineEdit.setText(text)

    # ---- 自动格式化 ----
    def _format_url(self):
        raw = self.urlLineEdit.text().strip()
        if not raw:
            self._paste_from_clipboard()
            raw = self.urlLineEdit.text().strip()
            if not raw:
                return

        url = normalize_github_url(raw)
        if url:
            self.urlLineEdit.setText(url)
        else:
            InfoBar.warning("格式化失败", "无法识别为 GitHub 仓库地址，请检查输入", duration=4000, parent=self)

    # ---- URL 验证 ----
    def _validate_url(self):
        if self._clone_running:
            self._start_btn.setEnabled(False)
            return
        url = self.urlLineEdit.text().strip()
        self._start_btn.setEnabled(bool(url))

    def repo_url(self) -> str:
        return self.urlLineEdit.text().strip()

    # ---- 开始克隆 ----
    def _on_start_clone(self):
        raw_url = self.urlLineEdit.text().strip()
        if not raw_url:
            InfoBar.warning("提示", "请输入仓库链接", parent=self)
            return

        normalized = normalize_github_url(raw_url) or raw_url
        repo_name = derive_repo_name(normalized)
        if not repo_name:
            InfoBar.warning("错误", "无法从 URL 解析仓库名称", parent=self)
            return

        base_abs = os.path.abspath(self._manager.base_dir)
        target_path = os.path.join(base_abs, repo_name)
        if os.path.exists(target_path):
            InfoBar.error("克隆失败", f"目录已存在: {repo_name}", duration=5000, parent=self)
            return

        # 清理上一次残留的线程
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None

        # 更新 UI 为克隆中状态
        self._clone_running = True
        self._set_cloning_ui(True)
        self._repo_name_label.setText(f"仓库名称: {repo_name}")
        self._clone_dir_label.setText(f"克隆目录: {target_path}")
        self._log_edit.clear()
        self._start_btn.setEnabled(False)
        self.urlLineEdit.setEnabled(False)
        self.paste_btn.setEnabled(False)
        self.format_btn.setEnabled(False)
        self._close_btn.setText("取消克隆")

        # 创建 Worker 并移入 QThread
        self._worker = CloneWorker(normalized, base_abs, repo_name)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.output.connect(self._on_output)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self._thread.start()

    def _set_cloning_ui(self, cloning: bool):
        self._progress_bar.setVisible(cloning)
        if cloning:
            self._progress_bar.start()
        else:
            self._progress_bar.stop()

    def _on_progress(self, phase: str):
        self._status_label.setText(f"状态: {phase}")

    def _on_output(self, text: str):
        self._log_edit.append(text)
        cursor = self._log_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._log_edit.setTextCursor(cursor)

    def _on_finished(self, success: bool, message: str):
        self._clone_running = False
        self._set_cloning_ui(False)

        worker = self._worker
        self._worker = None

        # 恢复 UI
        self._start_btn.setEnabled(True)
        self.urlLineEdit.setEnabled(True)
        self.paste_btn.setEnabled(True)
        self.format_btn.setEnabled(True)
        self._close_btn.setText("关闭")

        if success:
            self._status_label.setText("状态: 克隆完成")
            InfoBar.success("克隆成功", message, duration=4000, parent=self)
            # 自动添加到仓库列表
            if worker and os.path.isdir(os.path.join(
                os.path.abspath(os.path.join(worker._base_dir, worker._repo_name)), ".git"
            )):
                target_path = os.path.abspath(os.path.join(worker._base_dir, worker._repo_name))
                self._manager._add_repo_row(target_path)
                InfoBar.success(
                    "已添加", f"{worker._repo_name} 已添加到仓库列表",
                    duration=4000, parent=self._manager,
                )
            # 清空 URL 准备下一次克隆
            self.urlLineEdit.clear()
            self._repo_name_label.setText("仓库名称: -")
            self._clone_dir_label.setText("克隆目录: -")
            self._status_label.setText("状态: 就绪 — 可继续输入下一个仓库地址")
        else:
            self._status_label.setText("状态: 克隆失败")
            InfoBar.error("克隆失败", message, duration=5000, parent=self)

    # ---- 关闭 / 取消处理 ----
    def _on_close_clicked(self):
        if self._clone_running:
            box = MessageBox(
                "确认关闭", "克隆正在进行中，关闭窗口将中止克隆。\n确定要关闭吗？", self,
            )
            box.yesButton.setText("关闭并中止")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
            self._stop_clone()
        self.close()

    def _stop_clone(self):
        self._clone_running = False
        if self._worker:
            try:
                self._worker.progress.disconnect()
                self._worker.output.disconnect()
                self._worker.finished.disconnect()
            except Exception:
                pass
            self._worker.cancel()
            self._worker = None
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        # 恢复 UI
        self._set_cloning_ui(False)
        self._start_btn.setEnabled(True)
        self.urlLineEdit.setEnabled(True)
        self.paste_btn.setEnabled(True)
        self.format_btn.setEnabled(True)
        self._close_btn.setText("关闭")
        self._status_label.setText("状态: 已取消")

    def closeEvent(self, event):
        if self._clone_running:
            self._stop_clone()
        super().closeEvent(event)


class QtLogHandler(QObject):
    log_signal = Signal(str)

    def __init__(self, text_edit):
        super().__init__()
        self.text_edit = text_edit
        self.log_signal.connect(self._append_to_ui)

    def write(self, message: str):
        self.log_signal.emit(message)

    def _append_to_ui(self, message: str):
        self.text_edit.append(message.rstrip())
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.text_edit.setTextCursor(cursor)


# ====================== 版本历史弹窗 ======================
class HistoryDialog(QDialog):
    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.setWindowTitle(f"版本历史 - {os.path.basename(repo_path)}")
        self.resize(980, 680)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Commit", "日期", "作者", "提交信息"])
        self.table.verticalHeader().setVisible(False)  # 去掉左侧序号列

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换版本")
        self.switch_btn.clicked.connect(self.switch_to_version)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_history()

    def load_history(self):
        try:
            cmd = ["git", "log", "--pretty=format:%H|%ad|%an|%s", "--date=format:%Y-%m-%d %H:%M", "-30"]

            result = run_hidden(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                return

            self.table.setRowCount(0)
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                commit, date, author, message = line.split('|', 3)

                item_commit = QTableWidgetItem(commit[:12])  # 显示短 hash
                item_commit.setData(Qt.UserRole, commit)
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, item_commit)
                self.table.setItem(row, 1, QTableWidgetItem(date))
                self.table.setItem(row, 2, QTableWidgetItem(author))
                self.table.setItem(row, 3, QTableWidgetItem(message))

                if row == 0:
                    for col in range(4):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(0, 120, 215, 40))
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
        except Exception as e:
            logger.error(f"加载历史失败: {str(e)}")

    def on_double_click(self, item):
        self.switch_to_version()

    def switch_to_version(self):
        selected = self.table.selectedItems()
        if not selected:
            return

        row = selected[0].row()

        item = self.table.item(row, 0)

        # ✅ 优先用完整 commit
        commit = item.data(Qt.UserRole)
        if not commit:
            commit = item.text()  # fallback（防止旧数据）

        box = MessageBox(
            "确认切换版本", f"确定要切换到该版本？\n\nCommit: {commit[:12]}\n\n⚠ 此操作会丢弃所有未提交更改！", self
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_commit(self.repo_path, commit, self)


# ====================== 分支列表弹窗 ======================
class BranchDialog(QDialog):
    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.setWindowTitle(f"分支管理 - {os.path.basename(repo_path)}")
        self.resize(860, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["分支", "类型", "当前", "最新提交", "提交信息"])
        self.table.verticalHeader().setVisible(False)

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换分支")
        self.switch_btn.clicked.connect(self.switch_to_branch)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_branches()

    def load_branches(self):
        try:
            run_hidden(
                ["git", "fetch", "--quiet"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            current_result = run_hidden(
                ["git", "branch", "--show-current"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            current_branch = current_result.stdout.strip() if current_result.returncode == 0 else ""

            result = run_hidden(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(refname)|%(refname:short)|%(objectname:short)|%(committerdate:format:%Y-%m-%d %H:%M)|%(subject)",
                    "refs/heads",
                    "refs/remotes",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                return

            self.table.setRowCount(0)
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue

                refname, short_name, commit, date, message = line.split("|", 4)
                if refname.startswith("refs/remotes/") and refname.endswith("/HEAD"):
                    continue

                is_remote = refname.startswith("refs/remotes/")
                branch_type = "远程" if is_remote else "本地"
                is_current = not is_remote and short_name == current_branch

                branch_item = QTableWidgetItem(short_name)
                branch_item.setData(
                    Qt.UserRole,
                    {
                        "name": short_name,
                        "refname": refname,
                        "is_remote": is_remote,
                    },
                )

                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, branch_item)
                self.table.setItem(row, 1, QTableWidgetItem(branch_type))
                self.table.setItem(row, 2, QTableWidgetItem("✓" if is_current else ""))
                self.table.setItem(row, 3, QTableWidgetItem(f"{commit}  {date}".strip()))
                self.table.setItem(row, 4, QTableWidgetItem(message))

                if is_current:
                    for col in range(5):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(0, 120, 215, 40))
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
        except Exception as e:
            logger.error(f"加载分支失败: {str(e)}")

    def on_double_click(self, item):
        self.switch_to_branch()

    def switch_to_branch(self):
        selected = self.table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        item = self.table.item(row, 0)
        branch_info = item.data(Qt.UserRole) if item else None
        if not branch_info:
            return

        branch_name = branch_info["name"]
        branch_type = "远程分支" if branch_info["is_remote"] else "本地分支"

        box = MessageBox(
            "确认切换分支",
            f"确定要切换到该{branch_type}？\n\n分支: {branch_name}\n\n未提交的更改可能导致切换失败，请先确认工作区状态。",
            self,
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_branch(self.repo_path, branch_info, self)


# ====================== 主窗口 ======================
class GitManager(MSFluentWindow):
    update_row_signal = Signal(
        int, str, str, str, str, str, str, str
    )  # row, repo, branch, local, remote_commit, status, ahead_behind, remote_url
    notify_signal = Signal(str, str, str)
    scan_summary_signal = Signal(int, int, int)  # need_update_count, ignored_count, total_count
    update_complete_signal = Signal(str, bool, str)  # repo_name, success, message

    def __init__(self):
        super().__init__()
        qInitResources()
        setTheme(Theme.LIGHT)
        run_hidden(["git", "config", "--global", "core.quotepath", "false"], check=False)

        self.setWindowTitle("Git 多仓库管理器 - 高级版")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(1280, 800)

        self._config_cache = self._load_cached_config()
        self.base_dir = self.load_base_dir()
        self.repos: list[str] = []
        self._repo_cache: dict[str, dict] = {}  # path -> cached info
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._is_closing = False
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()
        self._scan_lock = threading.Lock()
        self._scan_generation = 0
        self._scan_expected = 0
        self._scan_done = 0
        self._scan_need_update = 0
        self._scan_ignored = 0

        self.init_ui()
        self.apply_proxy(self.load_proxy())

        self.qt_handler = QtLogHandler(self.log_text)
        logger.add(
            self.qt_handler.write, level="DEBUG", format="{time:HH:mm:ss} | <level>{level:8}</level> | {message}"
        )
        logger.success("Git 多仓库管理器启动成功")

    def _load_cached_config(self) -> dict:
        return load_config()

    def _save_cached_config(self):
        save_config(self._config_cache)

    def closeEvent(self, event):
        self._is_closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self._terminate_active_processes()
        # 保存仓库缓存
        save_repo_cache(list(self._repo_cache.values()))
        logger.remove()
        super().closeEvent(event)

    def center_window(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        screen_geometry = screen.availableGeometry()
        x = screen_geometry.x() + (screen_geometry.width() - self.width()) // 2
        y = screen_geometry.y() + (screen_geometry.height() - self.height()) // 2
        self.move(x, y)

    def load_base_dir(self) -> str:
        default_dir = os.getcwd()
        try:
            config = self._config_cache
            saved_dir = str(config.get("base_dir") or "").strip()
            if saved_dir and os.path.isdir(saved_dir):
                return saved_dir
        except Exception as e:
            logger.warning(f"加载配置失败: {str(e)}")

        return default_dir

    def load_proxy(self) -> str:
        config = self._config_cache
        return str(config.get("proxy") or "http://127.0.0.1:7897").strip()

    def load_token(self) -> str:
        config = self._config_cache
        return str(config.get("token") or "").strip()

    # ====================== 忽略更新管理 ======================
    def _get_ignore_list(self) -> list[str]:
        """获取忽略更新仓库路径列表（确保为 list 且元素为绝对路径）。"""
        raw = self._config_cache.get("ignore_update_repos")
        if not isinstance(raw, list):
            raw = []
        return [os.path.abspath(p) for p in raw if isinstance(p, str) and p.strip()]

    def is_repo_ignored(self, repo_path: str) -> bool:
        """判断指定仓库是否在忽略更新列表中。"""
        if not repo_path:
            return False
        return os.path.abspath(repo_path) in self._get_ignore_list()

    def ignore_repo_update(self, repo_path: str):
        """将仓库加入忽略更新列表并保存配置。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path not in ignore_list:
            ignore_list.append(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list
            self._save_cached_config()
            logger.info(f"[忽略更新] 已添加: {abs_path}")

    def restore_repo_update(self, repo_path: str):
        """将仓库从忽略更新列表移除并保存配置。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path in ignore_list:
            ignore_list.remove(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list
            self._save_cached_config()
            logger.info(f"[恢复更新] 已移除: {abs_path}")

    def _remove_ignored_record(self, repo_path: str):
        """删除仓库时同步清除忽略记录（静默操作，不保存配置）。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path in ignore_list:
            ignore_list.remove(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list

    def save_base_dir(self):
        try:
            self._config_cache["base_dir"] = self.base_dir
            self._save_cached_config()
        except Exception as e:
            logger.warning(f"保存配置失败: {str(e)}")

    def save_settings(self, base_dir: str, proxy: str, token: str):
        self._config_cache["base_dir"] = base_dir
        self._config_cache["proxy"] = proxy
        self._config_cache["token"] = token
        self._save_cached_config()

    def init_ui(self):
        self.repo_page = QWidget()
        self.repo_page.setObjectName("repo_page")
        repo_layout = QVBoxLayout(self.repo_page)
        repo_layout.setContentsMargins(16, 12, 16, 12)
        repo_layout.setSpacing(12)

        top = QHBoxLayout()
        self.dir_label = StrongBodyLabel(f"目录: {self.base_dir}")
        top.addWidget(self.dir_label)
        top.addStretch()

        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText("搜索仓库...")
        self.search_edit.setFixedWidth(200)
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_repos)
        top.addWidget(self.search_edit)

        self.scan_btn = PushButton(FIF.SYNC, "扫描仓库")
        self.scan_btn.clicked.connect(self.scan_repos)
        self.add_repo_btn = PushButton(FIF.ADD, "添加仓库")
        self.add_repo_btn.clicked.connect(self.show_clone_dialog)
        self.remove_repo_btn = PushButton(FIF.DELETE, "删除仓库")
        self.remove_repo_btn.clicked.connect(self.delete_selected_repo)
        self.bulk_update_btn = PrimaryPushButton(FIF.UPDATE, "一键更新")
        self.bulk_update_btn.clicked.connect(self.update_checked_repos)

        top.addWidget(self.scan_btn)
        top.addWidget(self.add_repo_btn)
        top.addWidget(self.remove_repo_btn)
        top.addWidget(self.bulk_update_btn)
        repo_layout.addLayout(top)

        repo_layout.addWidget(HorizontalSeparator())

        self.table = TableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["", "仓库名称", "当前分支", "当前版本", "最新版本", "状态 / 同步", "操作"])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)

        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 170)
        self.table.setColumnWidth(6, 90)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        self.table.itemClicked.connect(self.on_item_clicked)
        repo_layout.addWidget(self.table, 1)

        self.log_page = QWidget()
        self.log_page.setObjectName("log_page")
        log_page_layout = QVBoxLayout(self.log_page)
        log_page_layout.setContentsMargins(16, 12, 16, 12)
        log_page_layout.setSpacing(12)
        log_page_layout.addWidget(StrongBodyLabel("运行日志"))
        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        log_page_layout.addWidget(self.log_text, 1)

        self.settings_page = QWidget()
        self.settings_page.setObjectName("settings_page")
        settings_layout = QVBoxLayout(self.settings_page)
        settings_layout.setContentsMargins(24, 20, 24, 20)
        settings_layout.setSpacing(20)

        # ========== 目录设置 ==========
        self.dir_group = SettingCardGroup("存储目录", self.settings_page)
        self.dir_card = PushSettingCard(
            "选择目录",
            FIF.FOLDER,
            "仓库存放目录",
            "所有 Git 仓库将被存放在此目录下",
            self.dir_group,
        )
        self.dir_card.setContent(self.base_dir)
        self.dir_card.clicked.connect(self.select_settings_dir)
        self.dir_group.addSettingCard(self.dir_card)
        settings_layout.addWidget(self.dir_group)

        # ========== 网络设置 ==========
        self.network_group = SettingCardGroup("网络设置", self.settings_page)

        self.proxy_card = PushSettingCard(
            "点击编辑",
            FIF.GLOBE,
            "代理地址",
            "用于加速 Git 克隆和拉取，如 http://127.0.0.1:7897",
            self.network_group,
        )
        self.proxy_card.setContent(self.load_proxy() or "未设置")
        self.proxy_card.clicked.connect(self._edit_proxy)
        self.network_group.addSettingCard(self.proxy_card)

        self.token_card = PushSettingCard(
            "点击编辑",
            FIF.HEART,
            "访问令牌",
            "用于私有仓库认证，支持 GitHub / Gitee Token",
            self.network_group,
        )
        token = self.load_token()
        self.token_card.setContent(self._mask_token(token))
        self.token_card.clicked.connect(self._edit_token)
        self.network_group.addSettingCard(self.token_card)

        self.proxy_switch = SwitchSettingCard(
            FIF.POWER_BUTTON,
            "启用代理",
            content="关闭后 Git 操作将不使用代理",
            parent=self.network_group,
        )
        self.proxy_switch.setChecked(True)
        self.proxy_switch.checkedChanged.connect(self._on_proxy_toggle)
        self.network_group.addSettingCard(self.proxy_switch)

        settings_layout.addWidget(self.network_group)

        # ========== 关于 ==========
        self.about_group = SettingCardGroup("关于", self.settings_page)
        self.about_card = PushSettingCard(
            "查看",
            FIF.INFO,
            "Git 多仓库管理器",
            "高级版 · 支持批量管理、分支切换、版本回退",
            self.about_group,
        )
        self.about_card.clicked.connect(
            lambda: InfoBar.info("关于", "Git 多仓库管理器 v2.0\n基于 PySide6 + FluentWidgets 构建", parent=self)
        )
        self.about_group.addSettingCard(self.about_card)
        settings_layout.addWidget(self.about_group)

        # ========== 底部保存按钮 ==========
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 8, 0, 0)

        self.save_btn = PrimaryPushButton(FIF.SAVE, "保存设置")
        self.save_btn.setMinimumWidth(160)
        self.save_btn.clicked.connect(self.apply_settings_from_page)

        self.reset_btn = PushButton(FIF.CANCEL, "重置")
        self.reset_btn.setMinimumWidth(100)
        self.reset_btn.clicked.connect(self._reset_settings)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.reset_btn)
        bottom_layout.addWidget(self.save_btn)
        settings_layout.addWidget(bottom_widget)

        settings_layout.addStretch(1)

        self.addSubInterface(self.repo_page, FIF.HOME, "仓库")
        self.addSubInterface(self.log_page, FIF.DOCUMENT, "日志")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置", position=NavigationItemPosition.BOTTOM)

        self.update_row_signal.connect(self.update_table_row)
        self.notify_signal.connect(self.show_notification)
        self.scan_summary_signal.connect(self.show_scan_summary)
        self.update_complete_signal.connect(self.show_update_complete)

        # 启动后保持空闲，仓库列表只在用户点击“扫描仓库”后刷新。

    def _load_repo_cache_startup(self):
        """启动时不加载、不扫描、不刷新仓库列表。"""
        return

    # ====================== 工具方法 ======================
    def log_print(self, msg: str):
        logger.info(msg)

    def show_notification(self, level: str, title: str, content: str):
        if level == "success":
            InfoBar.success(title, content, parent=self)
        elif level == "error":
            InfoBar.error(title, content, parent=self)
        else:
            InfoBar.warning(title, content, parent=self)

    def run_git(self, path: str, args: list, timeout=60):
        return self.run_command(["git"] + args, cwd=path, timeout=timeout)

    def run_command(self, cmd: list[str], cwd: str | None = None, timeout=60, env: dict | None = None):
        if self._is_closing:
            return "", "Application is closing", -1

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

            return (stdout or "").strip(), (stderr or "").strip(), proc.returncode

        except Exception as e:
            logger.error(f"命令执行失败: {str(e)}")
            return "", str(e), -1

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

    def _terminate_active_processes(self):
        with self._process_lock:
            processes = list(self._active_processes)

        for proc in processes:
            self._terminate_process(proc)

    # ====================== 界面方法 ======================
    def select_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.base_dir)
        if path:
            self.base_dir = path
            self.dir_label.setText(f"目录: {self.base_dir}")
            self.save_base_dir()
            logger.info(f"切换目录 → {path}")

    def apply_proxy(self, proxy: str | None = None):
        if proxy is None:
            proxy = self.load_proxy()
        proxy = proxy.strip()
        if proxy:
            run_hidden(["git", "config", "--global", "http.proxy", proxy])
            logger.success(f"代理已设置: {proxy}")
        else:
            self.clear_proxy()

    def clear_proxy(self):
        run_hidden(["git", "config", "--global", "--unset", "http.proxy"])
        logger.success("代理已清除")

    def select_settings_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.base_dir)
        if path:
            self.dir_card.setContent(path)

    def apply_settings_from_page(self):
        base_dir = self.dir_card.contentLabel.text().strip() or self.base_dir
        proxy = self.proxy_card.contentLabel.text().strip()
        token = self.token_card.contentLabel.text().strip()
        proxy_enabled = self.proxy_switch.isChecked()

        if proxy == "未设置":
            proxy = ""

        if token == "未设置":
            token = ""

        self.base_dir = base_dir
        self.dir_label.setText(f"目录: {self.base_dir}")
        self.save_settings(base_dir, proxy, token)
        if proxy_enabled and proxy:
            self.apply_proxy(proxy)
        else:
            self.clear_proxy()
        logger.success("设置已保存")
        InfoBar.success("成功", "设置已保存并应用", parent=self)

    def _edit_proxy(self):
        dialog = MessageBoxBase(self)
        dialog.titleLabel = SubtitleLabel("编辑代理地址")
        dialog.cancelButton.setText("取消")
        dialog.yesButton.setText("确定")

        editor = LineEdit()
        editor.setText(self.load_proxy())
        editor.setClearButtonEnabled(True)
        editor.setPlaceholderText("例如: http://127.0.0.1:7897")
        dialog.viewLayout.addWidget(dialog.titleLabel)
        dialog.viewLayout.addWidget(editor)
        dialog.widget.setMinimumWidth(420)

        if dialog.exec():
            new_proxy = editor.text().strip()
            self.proxy_card.setContent(new_proxy or "未设置")

    def _edit_token(self):
        dialog = MessageBoxBase(self)
        dialog.titleLabel = SubtitleLabel("编辑访问令牌")
        dialog.cancelButton.setText("取消")
        dialog.yesButton.setText("确定")

        editor = LineEdit()
        editor.setText(self.load_token())
        editor.setClearButtonEnabled(True)
        editor.setPlaceholderText("支持 GitHub / Gitee Token")
        dialog.viewLayout.addWidget(dialog.titleLabel)
        dialog.viewLayout.addWidget(editor)
        dialog.widget.setMinimumWidth(420)

        if dialog.exec():
            new_token = editor.text().strip()
            self.token_card.setContent(self._mask_token(new_token))

    def _reset_settings(self):
        self.proxy_card.setContent(self.load_proxy() or "未设置")
        token = self.load_token()
        self.token_card.setContent(self._mask_token(token))
        self.dir_card.setContent(self.base_dir)
        self.proxy_switch.setChecked(True)
        InfoBar.info("提示", "已重置为当前保存的设置", parent=self)

    def _on_proxy_toggle(self, checked: bool):
        proxy = self.proxy_card.contentLabel.text().strip()
        if proxy == "未设置":
            proxy = ""
        if checked and proxy:
            self.apply_proxy(proxy)
        else:
            self.clear_proxy()

    @staticmethod
    def _mask_token(token: str) -> str:
        if not token:
            return "未设置"
        if len(token) <= 8:
            return token[:2] + "*" * (len(token) - 4) + token[-2:] if len(token) >= 4 else "****"
        return token[:4] + "*" * (len(token) - 8) + token[-4:]

    def _filter_repos(self, keyword: str):
        keyword = keyword.strip().lower()
        for row in range(self.table.rowCount()):
            repo_item = self.table.item(row, 1)
            if not repo_item:
                self.table.setRowHidden(row, bool(keyword))
                continue
            repo = repo_item.data(Qt.UserRole) or ""
            name = os.path.basename(repo.rstrip("\\/") or repo)
            self.table.setRowHidden(row, keyword not in name.lower())

    def scan_repos(self):
        self.table.setRowCount(0)
        self.repos.clear()
        self._repo_cache.clear()
        logger.info(f"开始扫描目录: {self.base_dir}")

        repo_candidates = scan_git_repos(self.base_dir)
        with self._scan_lock:
            self._scan_generation += 1
            generation = self._scan_generation
            self._scan_expected = len(repo_candidates)
            self._scan_done = 0
            self._scan_need_update = 0
            self._scan_ignored = 0

        for candidate in repo_candidates:
            self.repos.append(candidate.path)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c in range(7):
                self.table.setItem(row, c, QTableWidgetItem("..."))
            self._add_update_button(row)

        if not repo_candidates:
            self.scan_summary_signal.emit(0, 0, 0)
            return

        for i, repo in enumerate(self.repos):
            self.executor.submit(self.load_repo_info, i, repo, generation)

    def _mark_scan_progress(self, generation: int, need_update: bool, ignored: bool = False):
        with self._scan_lock:
            if generation != self._scan_generation:
                return

            self._scan_done += 1
            if need_update:
                self._scan_need_update += 1
            if ignored:
                self._scan_ignored += 1

            if self._scan_done == self._scan_expected:
                self.scan_summary_signal.emit(self._scan_need_update, self._scan_ignored, self._scan_expected)

    def show_scan_summary(self, need_update_count: int, ignored_count: int, total_count: int):
        parts = [f"共扫描 {total_count} 个仓库"]
        if need_update_count > 0:
            parts.append(f"需要更新 {need_update_count} 个")
        if ignored_count > 0:
            parts.append(f"已忽略更新 {ignored_count} 个")
        summary = "\n".join(parts)

        if need_update_count == 0 and ignored_count == 0:
            InfoBar.success("扫描完成", summary, duration=4000, parent=self)
        elif need_update_count == 0:
            InfoBar.success("扫描完成", summary, duration=5000, parent=self)
        else:
            InfoBar.warning("扫描完成", summary, duration=5000, parent=self)

    def show_update_complete(self, repo_name: str, success: bool, message: str):
        if success:
            InfoBar.success("更新成功", f"{repo_name} 已是最新版本", duration=4000, parent=self)
        else:
            InfoBar.error("更新失败", f"{repo_name} 更新失败。\n{message}", duration=5000, parent=self)

    def show_clone_dialog(self):
        """打开克隆仓库对话框，对话框内部管理整个克隆生命周期。"""
        dialog = CloneRepoDialog(self)
        dialog.exec()

    def get_selected_repo(self):
        row = self.table.currentRow()
        if row < 0:
            return None, None

        item = self.table.item(row, 0)
        if not item:
            return row, None

        repo_item = self.table.item(row, 1)
        repo = (repo_item.data(Qt.UserRole) if repo_item else "") or ""
        if not repo or repo == "...":
            return row, None

        return row, repo

    def delete_selected_repo(self):
        row, repo = self.get_selected_repo()
        if row is None or not repo:
            InfoBar.warning("提示", "请先选中一个仓库", parent=self)
            return

        repo_name = os.path.basename(repo.rstrip("\\/"))
        expected_target = os.path.abspath(os.path.join(os.path.abspath(self.base_dir), repo_name))
        selected_target = os.path.abspath(repo)
        if selected_target != expected_target:
            InfoBar.error("失败", "只允许删除当前基础目录下的直属仓库目录", parent=self)
            logger.error(f"拒绝删除非直属仓库目录: selected={selected_target}, expected={expected_target}")
            return

        box = MessageBox("确认删除仓库", f"确定删除本地仓库？\n\n{repo}", self)
        box.yesButton.setText("删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        try:
            result = safe_remove_repo_dir(self.base_dir, repo_name, onerror=self._handle_remove_readonly)
            if result["success"]:
                logger.success(f"已删除仓库: {selected_target}")
                # 直接从表格和缓存移除该行
                self.table.removeRow(row)
                try:
                    self.repos.remove(repo)
                except ValueError:
                    pass
                self._remove_ignored_record(repo)
                self._save_cached_config()
                InfoBar.success("成功", "仓库已删除", parent=self)
            else:
                logger.error(f"删除仓库失败: {result['error']}")
                InfoBar.error("失败", (result["error"] or "删除失败")[:150], parent=self)
        except Exception as e:
            logger.error(f"删除仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    @staticmethod
    def _handle_remove_readonly(func, path, exc):
        # exc 在 onexc 回调中是异常对象本身，在旧 onerror 中是 (type, value, tb) 元组
        actual_exc = exc[1] if isinstance(exc, tuple) else exc
        if isinstance(actual_exc, OSError):
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        raise actual_exc

    def clone_repo(self, repo_input: str | GitRepoInfo) -> dict:
        result = self.clone_git_repo(repo_input, self.base_dir)
        repo_name = derive_repo_name(repo_input)
        target_path = os.path.abspath(os.path.join(self.base_dir, repo_name)) if repo_name else ""

        if result["success"]:
            logger.success(f"仓库克隆成功: {target_path or repo_name}")
            if target_path and os.path.isdir(os.path.join(target_path, ".git")):
                self._add_repo_row(target_path)
                self.notify_signal.emit("success", "克隆成功", f"{repo_name} 已添加到列表")
            else:
                self.notify_signal.emit("success", "克隆成功", f"仓库已克隆到: {target_path}")
        else:
            error_message = result["error"] or "克隆失败"
            logger.error(f"仓库克隆失败: {error_message}")
            self.notify_signal.emit("error", "克隆失败", error_message[:200])

        return result

    def clone_git_repo(self, repo_input: str | GitRepoInfo, base_dir: str) -> dict:
        repo_name = derive_repo_name(repo_input)
        base_abs = os.path.abspath(base_dir)
        target_path = os.path.abspath(os.path.join(base_abs, repo_name)) if repo_name else ""

        if not repo_name:
            return {"success": False, "error": "无法从 URL 解析仓库名称"}

        if not os.path.exists(base_abs):
            return {"success": False, "error": "base_dir 不存在"}

        base_prefix = base_abs.rstrip("\\/") + os.sep
        if not target_path.startswith(base_prefix):
            return {"success": False, "error": "clone 目标路径不安全"}

        if os.path.exists(target_path):
            return {"success": False, "error": f"目录已存在: {repo_name}"}

        candidates = build_clone_candidates(repo_input)
        if not candidates:
            return {"success": False, "error": "仓库地址不能为空"}

        errors = []
        for candidate in candidates:
            logger.info(f"[克隆] 尝试源: {candidate}")
            out, err, code = self.run_command(
                ["git", "clone", candidate, target_path],
                cwd=base_abs,
                timeout=600,
            )

            if code == 0:
                return {"success": True, "error": None}

            errors.append(f"{candidate} -> {err or out or '克隆失败'}")
            if os.path.exists(target_path):
                try:
                    rmtree(target_path, onerror=self._handle_remove_readonly)
                except Exception as cleanup_error:
                    errors.append(f"清理失败: {str(cleanup_error)}")
                    break

        return {"success": False, "error": " | ".join(errors)[:500] or "克隆失败"}

    def _add_update_button(self, row: int):
        """创建更新按钮（只显示图标）"""
        btn = ToolButton(FIF.UPDATE)
        btn.setFixedWidth(85)
        ToolTipFilter(btn, showDelay=300, position=ToolTipPosition.TOP)
        btn.setToolTip("更新仓库")
        btn.clicked.connect(lambda _, r=row: self.update_single_repo(r))
        self.table.takeItem(row, 6)
        self.table.setCellWidget(row, 6, btn)
        return btn

    def _add_repo_row(self, repo_path: str):
        """直接添加一行仓库到表格，不重新扫描"""
        repo_path = os.path.abspath(repo_path)
        if repo_path in self.repos:
            return
        self.repos.append(repo_path)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for c in range(7):
            self.table.setItem(row, c, QTableWidgetItem("..."))
        self._add_update_button(row)
        # 撤销搜索过滤确保新行可见
        self.search_edit.clear()
        # 加载仓库信息
        self.executor.submit(self.load_repo_info, row, repo_path, None)

    def update_single_repo(self, row: int):
        item = self.table.item(row, 1)
        repo = item.data(Qt.UserRole) if item else ""
        if repo:
            self.executor.submit(self.pull_repo, repo, row)

    def get_checked_repos(self) -> list[tuple[int, str]]:
        checked: list[tuple[int, str]] = []
        for row in range(self.table.rowCount()):
            check_item = self.table.item(row, 0)
            repo_item = self.table.item(row, 1)
            if not check_item or not repo_item:
                continue
            repo = repo_item.data(Qt.UserRole)
            if repo and check_item.checkState() == Qt.Checked:
                checked.append((row, repo))
        return checked

    def update_checked_repos(self):
        checked_repos = self.get_checked_repos()
        if not checked_repos:
            InfoBar.warning("提示", "请先勾选要更新的仓库", parent=self)
            return

        skipped = 0
        to_update = []
        for row, repo in checked_repos:
            if self.is_repo_ignored(repo):
                repo_name = os.path.basename(repo.rstrip("\\/"))
                logger.info(f"[跳过更新] {repo_name} 已设置忽略更新")
                skipped += 1
            else:
                to_update.append((row, repo))

        if skipped:
            InfoBar.warning("跳过已忽略", f"已跳过 {skipped} 个忽略更新的仓库", duration=4000, parent=self)

        if not to_update:
            if skipped:
                InfoBar.warning("提示", "选中的仓库均已忽略更新，无可执行项", parent=self)
            return

        for row, repo in to_update:
            self.executor.submit(self.pull_repo, repo, row)
        logger.info(f"[批量更新] 已提交 {len(to_update)} 个仓库")
        InfoBar.success("已开始", f"正在更新 {len(to_update)} 个仓库" + (f"（跳过 {skipped} 个已忽略）" if skipped else ""), parent=self)

    def on_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return

        row = item.row()
        repo_item = self.table.item(row, 1)
        repo = repo_item.data(Qt.UserRole) if repo_item else ""
        if not repo or repo == "...":
            return

        menu = RoundMenu(parent=self)

        menu.addAction(Action(FIF.FOLDER, "打开本地", triggered=lambda: self._open_local_folder(repo)))

        cache = self._repo_cache.get(repo, {})
        remote_url = cache.get("remote_url", "")
        open_remote = Action(FIF.GLOBE, "打开远端", triggered=lambda: self._open_remote_url(repo))
        if not remote_url:
            open_remote.setEnabled(False)
        menu.addAction(open_remote)

        menu.addSeparator()

        # 忽略更新 / 恢复更新（动态显示）
        if self.is_repo_ignored(repo):
            menu.addAction(Action(FIF.PLAY, "继续更新", triggered=lambda: self._toggle_ignore_update(row, repo, ignore=False)))
        else:
            menu.addAction(Action(FIF.PAUSE, "忽略更新", triggered=lambda: self._toggle_ignore_update(row, repo, ignore=True)))

        menu.addSeparator()

        menu.addAction(Action(FIF.DELETE, "删除仓库", triggered=lambda: self._context_delete_repo(row, repo)))

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _toggle_ignore_update(self, row: int, repo: str, ignore: bool):
        """切换仓库的忽略更新状态。"""
        repo_name = os.path.basename(repo.rstrip("\\/"))
        if ignore:
            self.ignore_repo_update(repo)
            InfoBar.success("忽略更新", f"{repo_name} 仓库已忽略更新检查", duration=4000, parent=self)
        else:
            self.restore_repo_update(repo)
            InfoBar.success("恢复更新", f"{repo_name} 仓库已恢复更新检查", duration=4000, parent=self)
        # 立即刷新当前仓库状态
        self.executor.submit(self.load_repo_info, row, repo, None)

    def _open_local_folder(self, repo: str):
        path = os.path.abspath(repo)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        logger.info(f"打开本地目录: {path}")

    def _open_remote_url(self, repo: str):
        cache = self._repo_cache.get(repo, {})
        remote_url = cache.get("remote_url", "")
        if not remote_url:
            InfoBar.warning("提示", "未找到远端地址", parent=self)
            return

        webbrowser.open(remote_url)
        logger.info(f"打开远端地址: {remote_url}")

    def _context_delete_repo(self, row: int, repo: str):
        repo_name = os.path.basename(repo.rstrip("\\/"))
        expected_target = os.path.abspath(os.path.join(os.path.abspath(self.base_dir), repo_name))
        selected_target = os.path.abspath(repo)
        if selected_target != expected_target:
            InfoBar.error("失败", "只允许删除当前基础目录下的直属仓库目录", parent=self)
            return

        box = MessageBox("确认删除仓库", f"确定删除本地仓库？\n\n{repo}", self)
        box.yesButton.setText("删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        try:
            result = safe_remove_repo_dir(self.base_dir, repo_name, onerror=self._handle_remove_readonly)
            if result["success"]:
                logger.success(f"已删除仓库: {selected_target}")
                self.table.removeRow(row)
                try:
                    self.repos.remove(repo)
                except ValueError:
                    pass
                self._repo_cache.pop(repo, None)
                self._remove_ignored_record(repo)
                self._save_cached_config()
                InfoBar.success("成功", "仓库已删除", parent=self)
            else:
                logger.error(f"删除仓库失败: {result['error']}")
                InfoBar.error("失败", (result["error"] or "删除失败")[:150], parent=self)
        except Exception as e:
            logger.error(f"删除仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    # ====================== 数据加载与按钮状态控制 ======================
    def load_repo_info(self, row: int, repo: str, generation: int | None = None):
        need_update = False
        ignored = False
        try:
            local, _, _ = self.run_git(repo, ["rev-parse", "--short", "HEAD"])
            branch, _, _ = self.run_git(repo, ["branch", "--show-current"])
            if not branch:
                branch = "游离 HEAD"

            # 获取远端地址
            remote_url, _, _ = self.run_git(repo, ["remote", "get-url", "origin"])

            # 忽略更新的仓库：跳过 fetch 和远程检查
            if self.is_repo_ignored(repo):
                ignored = True
                status = "⏸ 已忽略更新"
                ahead_behind = "-"
                remote_commit = "N/A"
            else:
                self.run_git(repo, ["fetch", "--quiet"])

                ahead, _, _ = self.run_git(repo, ["rev-list", "--count", "HEAD", "^@{u}"])
                behind, _, _ = self.run_git(repo, ["rev-list", "--count", "@{u}", "^HEAD"])

                ahead = int(ahead) if ahead.isdigit() else 0
                behind = int(behind) if behind.isdigit() else 0

                remote_commit, _, rc = self.run_git(repo, ["rev-parse", "--short", "@{u}"])
                if rc != 0:
                    status = "错误"
                    ahead_behind = "N/A"
                elif ahead == 0 and behind == 0:
                    status = "✓ 已同步"
                    ahead_behind = "✓"
                else:
                    status = "可更新"
                    ahead_behind = f"↑{ahead} ↓{behind}"
                    need_update = True

            self.update_row_signal.emit(row, repo, branch, local or "N/A", remote_commit or "N/A", status, ahead_behind, remote_url or "")
        except Exception as e:
            logger.error(f"加载失败 {repo}: {str(e)}")
            self.update_row_signal.emit(row, repo, "N/A", "错误", "N/A", "错误", "N/A", "")
        finally:
            if generation is not None:
                self._mark_scan_progress(generation, need_update, ignored)

    def update_table_row(
        self, row: int, repo: str, branch: str, local: str, remote_commit: str, status: str, ahead_behind: str, remote_url: str = ""
    ):
        if row >= self.table.rowCount():
            return

        old_check_item = self.table.item(row, 0)
        old_check_state = old_check_item.checkState() if old_check_item else Qt.Unchecked

        check_item = QTableWidgetItem("")
        is_updatable = status == "可更新"
        is_ignored = status == "⏸ 已忽略更新"
        if is_updatable:
            check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check_item.setCheckState(old_check_state)
        else:
            check_item.setFlags(Qt.NoItemFlags)
            check_item.setCheckState(Qt.Unchecked)
        check_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 0, check_item)

        repo_name = os.path.basename(repo.rstrip("\\/")) or repo
        repo_item = QTableWidgetItem(repo_name)
        repo_item.setData(Qt.UserRole, repo)
        repo_item.setToolTip(repo)
        self.table.setItem(row, 1, repo_item)

        status_text = f"{status}  {ahead_behind}" if ahead_behind not in ("N/A", "✓", "-") else status
        for col, text in enumerate([branch, local, remote_commit, status_text]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col + 2, item)

        # 状态颜色
        status_item = self.table.item(row, 5)
        if status == "可更新":
            status_item.setForeground(QColor("#ff9800"))
        elif status == "✓ 已同步":
            status_item.setForeground(QColor("#00c853"))
        elif is_ignored:
            status_item.setForeground(QColor("#9e9e9e"))

        # 按钮状态控制
        btn = self.table.cellWidget(row, 6)
        if btn:
            btn.setEnabled(is_updatable)
            if not is_updatable:
                btn.setStyleSheet("opacity: 0.5;")
            else:
                btn.setStyleSheet("")

        # 更新缓存
        self._repo_cache[repo] = {
            "name": repo_name,
            "path": repo,
            "remote_url": remote_url or "",
            "branch": branch,
            "local": local,
            "remote": remote_commit,
            "status": status,
            "ahead_behind": ahead_behind,
        }

    # ====================== 更新逻辑 ======================
    def pull_repo(self, repo: str, row: int):
        repo_name = os.path.basename(repo.rstrip("\\/"))
        logger.info(f"[更新] {repo}")

        if self.is_repo_ignored(repo):
            logger.info(f"[跳过更新] {repo_name} 已设置忽略更新")
            self.update_complete_signal.emit(repo_name, False, "该仓库已设置忽略更新")
            return

        if os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")):
            logger.warning("存在未完成 merge，跳过")
            self.update_complete_signal.emit(repo_name, False, "存在未完成的 merge，请先处理后再更新。")
            return

        clean, _, _ = self.run_git(repo, ["status", "--porcelain"])
        if clean.strip():
            logger.warning("工作区有未提交更改，跳过")
            self.update_complete_signal.emit(repo_name, False, "工作区有未提交的更改，请先提交或暂存后再更新。")
            return

        out, err, code = self.run_git(repo, ["-c", "core.editor=true", "pull", "--rebase"])

        self.load_repo_info(row, repo)

        if code == 0:
            logger.success(f"{repo} 更新成功")
            self.update_complete_signal.emit(repo_name, True, "")
        else:
            logger.error(f"{repo} 更新失败\n{err}")
            self.update_complete_signal.emit(repo_name, False, err[:200] if err else "更新过程中出现未知错误。")

    def on_item_clicked(self, item):
        row = item.row()
        col = item.column()
        repo_item = self.table.item(row, 1)
        repo = repo_item.data(Qt.UserRole) if repo_item else ""

        if col == 2:  # 当前分支 → 分支管理
            BranchDialog(repo, self).exec()
        elif col == 3:  # 当前版本 → 历史
            HistoryDialog(repo, self).exec()

    def switch_to_commit(self, repo: str, commit: str, dialog=None):
        logger.warning(f"硬重置 {repo} → {commit}")
        _, err, code = self.run_git(repo, ["reset", "--hard", commit])

        if code == 0:
            logger.success("重置成功")
            InfoBar.success("成功", f"已切换到 {commit[:12]}", parent=self)
        else:
            logger.error(f"重置失败: {err}")
            InfoBar.error("失败", err[:150], parent=self)

        try:
            row = self.repos.index(repo)
            self.load_repo_info(row, repo)
        except ValueError:
            pass

        if dialog:
            dialog.close()

    def switch_to_branch(self, repo: str, branch_info: dict, dialog=None):
        branch_name = branch_info.get("name", "")
        is_remote = branch_info.get("is_remote", False)
        if not branch_name:
            InfoBar.warning("提示", "分支名称为空", parent=self)
            return

        current_branch, _, _ = self.run_git(repo, ["branch", "--show-current"])
        if current_branch == branch_name:
            InfoBar.warning("提示", "当前已经在该分支", parent=self)
            if dialog:
                dialog.close()
            return

        logger.info(f"切换分支 {repo} → {branch_name}")

        if is_remote:
            local_branch = branch_name.split("/", 1)[1] if "/" in branch_name else branch_name
            local_match, _, _ = self.run_git(repo, ["branch", "--list", local_branch])
            if local_match.strip():
                switch_args = ["switch", local_branch]
            else:
                switch_args = ["switch", "--track", branch_name]
        else:
            switch_args = ["switch", branch_name]

        _, err, code = self.run_git(repo, switch_args)

        if code == 0:
            logger.success(f"分支切换成功: {branch_name}")
            InfoBar.success("成功", f"已切换到 {branch_name}", parent=self)
        else:
            logger.error(f"分支切换失败: {err}")
            InfoBar.error("失败", (err or "分支切换失败")[:150], parent=self)

        try:
            row = self.repos.index(repo)
            self.load_repo_info(row, repo)
        except ValueError:
            pass

        if dialog and code == 0:
            dialog.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitManager()
    window.center_window()
    window.show()
    sys.exit(app.exec())
