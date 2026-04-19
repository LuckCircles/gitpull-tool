import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from loguru import logger
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    HorizontalSeparator,
    InfoBar,
    LineEdit,
    ListWidget,
    MessageBox,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TextEdit,
    Theme,
    ToolButton,
    setTheme,
)
from qfluentwidgets import FluentIcon as FIF

from res_rc import qInitResources

logger.remove()
logger.add("git_manager_{time:YYYY-MM-DD}.log", rotation="10 MB", retention="7 days", encoding="utf-8")
CONFIG_FILE = Path(__file__).with_name("config.json")


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


def safe_remove_repo_dir(base_path: str, repo_name: str, onexc=None) -> dict:
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
        shutil.rmtree(target_abs, onexc=onexc)
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


class DiffDialog(QDialog):
    def __init__(self, repo: str, sections: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Diff - {repo}")
        self.resize(900, 600)

        layout = QHBoxLayout(self)

        # 左侧导航
        self.nav = ListWidget()
        for key in sections.keys():
            self.nav.addItem(key)

        # 右侧内容
        self.viewer = TextEdit()
        self.viewer.setReadOnly(True)
        # self.viewer.setFont(QFont("Consolas", 10))

        layout.addWidget(self.nav, 1)
        layout.addWidget(self.viewer, 4)

        self.sections = sections
        self.nav.currentTextChanged.connect(self.update_view)

        # 默认选中第一个
        if sections:
            self.nav.setCurrentRow(0)

    def update_view(self, key):
        text = self.sections.get(key, "")
        self.viewer.setHtml(self.format_diff_html(text))

    @staticmethod
    def format_diff_html(text: str) -> str:
        lines = []

        for raw_line in text.splitlines():
            escaped = html.escape(raw_line)

            if raw_line.startswith("+++") or raw_line.startswith("---"):
                color = "#8e8e93"
            elif raw_line.startswith("+"):
                color = "#16a34a"
            elif raw_line.startswith("-"):
                color = "#dc2626"
            elif raw_line.startswith("@@"):
                color = "#2563eb"
            else:
                color = "#f5f5f5"

            lines.append(f'<span style="color: {color};">{escaped}</span>')

        return "<br>".join(lines) if lines else ""


class CloneRepoDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("克隆仓库")
        self.urlLineEdit = LineEdit()

        self.urlLineEdit.setPlaceholderText("输入 Git 仓库链接")
        self.urlLineEdit.setClearButtonEnabled(True)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.urlLineEdit)

        # ========== 新增：Git 链接实时验证 ==========
        self.urlLineEdit.textChanged.connect(self._validate_url)
        self.yesButton.setEnabled(False)  # 初始禁用“克隆”按钮
        # ===========================================

        self.widget.setMinimumWidth(420)
        self.yesButton.setText("克隆")
        self.cancelButton.setText("取消")

    def repo_url(self) -> str:
        return self.urlLineEdit.text().strip()

    # ========== 新增：Git 链接验证方法 ==========
    def _validate_url(self):
        """实时验证输入的 Git 仓库链接"""
        url = self.urlLineEdit.text().strip()
        if self._is_valid_git_url(url):
            self.yesButton.setEnabled(True)
            # 恢复正常样式（如果之前有错误提示）
            self.urlLineEdit.setStyleSheet("")
        else:
            self.yesButton.setEnabled(False)
            # 可选：添加红色边框提示错误（qfluentwidgets 的 LineEdit 支持）
            # self.urlLineEdit.setStyleSheet("border: 1px solid #e74c3c;")

    @staticmethod
    def _is_valid_git_url(url: str) -> bool:
        """Git 仓库链接验证（支持 HTTPS、SSH、Git 协议等常见格式）"""
        if not url:
            return False

        # 推荐的 Git URL 正则（经过实际项目验证，能覆盖绝大部分合法场景）
        pattern = re.compile(
            r'^(?:(?:https?|git|ssh)://'  # 协议
            r'(?:[^@]+@)?'  # 可选用户名
            r'[\w.-]+'  # 主机名
            r'(?::\d+)?'  # 可选端口
            r'[:/]'  # 分隔符
            r'[\w./-]+?'  # 路径
            r'(?:\.git)?/?)$',  # 可选 .git 后缀
            re.IGNORECASE,
        )
        return bool(pattern.match(url))


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


# ====================== 主窗口 ======================
class GitManager(QMainWindow):
    update_row_signal = Signal(int, str, str, str, str, str)  # row, repo, local, remote, status, ahead_behind
    notify_signal = Signal(str, str, str)
    refresh_repos_signal = Signal()

    def __init__(self):
        super().__init__()
        qInitResources()
        setTheme(Theme.LIGHT)
        run_hidden(["git", "config", "--global", "core.quotepath", "false"], check=False)

        self.setWindowTitle("Git 多仓库管理器 - 高级版")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(1280, 800)

        self.base_dir = self.load_base_dir()
        self.repos: list[str] = []
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._is_closing = False
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()

        self.init_ui()

        self.qt_handler = QtLogHandler(self.log_text)
        logger.add(
            self.qt_handler.write, level="DEBUG", format="{time:HH:mm:ss} | <level>{level:8}</level> | {message}"
        )
        logger.success("Git 多仓库管理器启动成功")

    def closeEvent(self, event):
        self._is_closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self._terminate_active_processes()
        logger.remove()
        super().closeEvent(event)

    def load_base_dir(self) -> str:
        default_dir = os.getcwd()
        try:
            if not CONFIG_FILE.exists():
                return default_dir

            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                config = json.load(f)

            saved_dir = str(config.get("base_dir") or "").strip()
            if saved_dir and os.path.isdir(saved_dir):
                return saved_dir
        except Exception as e:
            logger.warning(f"加载配置失败: {str(e)}")

        return default_dir

    def save_base_dir(self):
        try:
            with CONFIG_FILE.open("w", encoding="utf-8") as f:
                json.dump({"base_dir": self.base_dir}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存配置失败: {str(e)}")

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 顶部
        top = QHBoxLayout()
        self.dir_label = StrongBodyLabel(f"目录: {self.base_dir}")
        top.addWidget(self.dir_label)
        top.addStretch()

        self.select_btn = PushButton(FIF.FOLDER, "选择目录")
        self.select_btn.clicked.connect(self.select_folder)
        self.scan_btn = PushButton(FIF.SYNC, "扫描仓库")
        self.scan_btn.clicked.connect(self.scan_repos)
        self.add_repo_btn = PushButton(FIF.ADD, "添加仓库")
        self.add_repo_btn.clicked.connect(self.show_clone_dialog)
        self.remove_repo_btn = PushButton(FIF.DELETE, "删除仓库")
        self.remove_repo_btn.clicked.connect(self.delete_selected_repo)

        top.addWidget(self.select_btn)
        top.addWidget(self.scan_btn)
        top.addWidget(self.add_repo_btn)
        top.addWidget(self.remove_repo_btn)
        layout.addLayout(top)

        # 代理
        proxy_layout = QHBoxLayout()
        proxy_layout.addWidget(BodyLabel("代理:"))
        self.proxy_entry = LineEdit()
        self.proxy_entry.setText("http://127.0.0.1:7897")
        self.proxy_entry.setMinimumWidth(340)
        proxy_layout.addWidget(self.proxy_entry)

        apply_btn = PushButton(FIF.SEND, "应用代理")
        apply_btn.clicked.connect(self.apply_proxy)
        clear_btn = PushButton(FIF.CLEAR_SELECTION, "清除代理")
        clear_btn.clicked.connect(self.clear_proxy)
        proxy_layout.addWidget(apply_btn)
        proxy_layout.addWidget(clear_btn)
        proxy_layout.addStretch()
        layout.addLayout(proxy_layout)

        layout.addWidget(HorizontalSeparator())

        # 表格
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["仓库路径", "当前版本", "最新版本", "状态 / 同步", "操作"])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)

        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 170)
        self.table.setColumnWidth(4, 90)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.itemClicked.connect(self.on_item_clicked)

        layout.addWidget(self.table, 1)

        # 日志
        log_layout = QVBoxLayout()
        log_layout.addWidget(StrongBodyLabel("运行日志"))
        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        layout.addLayout(log_layout, 1)

        self.update_row_signal.connect(self.update_table_row)
        self.notify_signal.connect(self.show_notification)
        self.refresh_repos_signal.connect(self.scan_repos)

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

    def apply_proxy(self):
        proxy = self.proxy_entry.text().strip()
        if proxy:
            run_hidden(["git", "config", "--global", "http.proxy", proxy])
            logger.success(f"代理已设置: {proxy}")

    def clear_proxy(self):
        run_hidden(["git", "config", "--global", "--unset", "http.proxy"])
        logger.success("代理已清除")

    def scan_repos(self):
        self.table.setRowCount(0)
        self.repos.clear()
        logger.info(f"开始扫描目录: {self.base_dir}")

        repo_candidates = scan_git_repos(self.base_dir)
        for candidate in repo_candidates:
            self.repos.append(candidate.path)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c in range(5):
                self.table.setItem(row, c, QTableWidgetItem("..."))
            self._add_update_button(row)

        for i, repo in enumerate(self.repos):
            self.executor.submit(self.load_repo_info, i, repo)

    def show_clone_dialog(self):
        dialog = CloneRepoDialog(self)
        if not dialog.exec():
            return

        repo_url = dialog.repo_url()
        if not repo_url:
            InfoBar.warning("提示", "请输入仓库链接", parent=self)
            return

        self.executor.submit(self.clone_repo, repo_url)

    def get_selected_repo(self):
        row = self.table.currentRow()
        if row < 0:
            return None, None

        item = self.table.item(row, 0)
        if not item:
            return row, None

        repo = item.text().strip()
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
            result = safe_remove_repo_dir(self.base_dir, repo_name, onexc=self._handle_remove_readonly)
            if result["success"]:
                logger.success(f"已删除仓库: {selected_target}")
                InfoBar.success("成功", "仓库已删除", parent=self)
                self.scan_repos()
            else:
                logger.error(f"删除仓库失败: {result['error']}")
                InfoBar.error("失败", (result["error"] or "删除失败")[:150], parent=self)
        except Exception as e:
            logger.error(f"删除仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    @staticmethod
    def _handle_remove_readonly(func, path, exc_info):
        exc = exc_info[1]
        if isinstance(exc, PermissionError):
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        raise exc

    def clone_repo(self, repo_input: str | GitRepoInfo) -> dict:
        result = self.clone_git_repo(repo_input, self.base_dir)
        repo_name = derive_repo_name(repo_input)
        target_path = os.path.abspath(os.path.join(self.base_dir, repo_name)) if repo_name else ""

        if result["success"]:
            success_message = f"已克隆到: {target_path}" if target_path else "仓库克隆成功"
            logger.success(f"仓库克隆成功: {target_path or repo_name}")
            self.notify_signal.emit("success", "成功", success_message)
            self.refresh_repos_signal.emit()
        else:
            error_message = result["error"] or "克隆失败"
            logger.error(f"仓库克隆失败: {error_message}")
            self.notify_signal.emit("error", "失败", error_message[:200])

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
                    shutil.rmtree(target_path, onexc=self._handle_remove_readonly)
                except Exception as cleanup_error:
                    errors.append(f"清理失败: {str(cleanup_error)}")
                    break

        return {"success": False, "error": " | ".join(errors)[:500] or "克隆失败"}

    def _add_update_button(self, row: int):
        """创建更新按钮（只显示图标）"""
        btn = ToolButton(FIF.UPDATE)
        btn.setFixedWidth(85)
        btn.clicked.connect(lambda _, r=row: self.update_single_repo(r))
        self.table.setCellWidget(row, 4, btn)
        return btn

    def update_single_repo(self, row: int):
        repo = self.table.item(row, 0).text()
        if repo:
            self.executor.submit(self.pull_repo, repo, row)

    def on_item_clicked(self, item):
        row = item.row()
        col = item.column()
        repo = self.table.item(row, 0).text()

        if col == 1:  # 当前版本 → 历史
            HistoryDialog(repo, self).exec()
        elif col == 2:  # 最新版本 → diff
            self.show_diff(repo)

    def show_diff(self, repo: str):
        def run(args):
            return self.run_git(repo, args)

        sections = {}

        # 工作区
        out, _, _ = run(["diff"])
        if out.strip():
            sections["📂 工作区变更"] = out

        # 暂存区
        out, _, _ = run(["diff", "--cached"])
        if out.strip():
            sections["📌 已暂存"] = out

        # upstream 检查
        _, _, code = run(["rev-parse", "--symbolic-full-name", "@{u}"])
        has_upstream = code == 0
        if code == 0:
            out, _, _ = run(["diff", "HEAD", "@{u}"])
            if out.strip():
                sections["🌐 本地 vs 远程"] = out

            out, _, _ = run(["log", "--oneline", "@{u}..HEAD"])
            if out.strip():
                sections["⬆ 本地领先"] = out

            out, _, _ = run(["log", "--oneline", "HEAD..@{u}"])
            if out.strip():
                sections["⬇ 远程领先"] = out
        else:
            sections["⚠ 信息"] = "未设置 upstream"

        if not sections:
            QMessageBox.information(self, "Diff", "无差异")
            return

        dlg = DiffDialog(repo, sections, self)
        dlg.exec()

    # ====================== 数据加载与按钮状态控制 ======================
    def load_repo_info(self, row: int, repo: str):
        try:
            local, _, _ = self.run_git(repo, ["rev-parse", "--short", "HEAD"])
            self.run_git(repo, ["fetch", "--quiet"])

            ahead, _, _ = self.run_git(repo, ["rev-list", "--count", "HEAD", "^@{u}"])
            behind, _, _ = self.run_git(repo, ["rev-list", "--count", "@{u}", "^HEAD"])

            ahead = int(ahead) if ahead.isdigit() else 0
            behind = int(behind) if behind.isdigit() else 0

            remote, _, rc = self.run_git(repo, ["rev-parse", "--short", "@{u}"])
            if rc != 0:
                status = "错误"
                ahead_behind = "N/A"
            elif ahead == 0 and behind == 0:
                status = "✓ 已同步"
                ahead_behind = "✓"
            else:
                status = "可更新"
                ahead_behind = f"↑{ahead} ↓{behind}"

            self.update_row_signal.emit(row, repo, local or "N/A", remote or "N/A", status, ahead_behind)
        except Exception as e:
            logger.error(f"加载失败 {repo}: {str(e)}")
            self.update_row_signal.emit(row, repo, "错误", "N/A", "错误", "N/A")

    def update_table_row(self, row: int, repo: str, local: str, remote: str, status: str, ahead_behind: str):
        if row >= self.table.rowCount():
            return

        self.table.setItem(row, 0, QTableWidgetItem(repo))

        for col, text in enumerate(
            [local, remote, f"{status}  {ahead_behind}" if ahead_behind not in ("N/A", "✓") else status]
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col + 1, item)

        # 状态颜色
        status_item = self.table.item(row, 3)
        if status == "可更新":
            status_item.setForeground(QColor("#ff9800"))
        elif status == "✓ 已同步":
            status_item.setForeground(QColor("#00c853"))

        # ====================== 关键修改：按钮状态控制 ======================
        btn = self.table.cellWidget(row, 4)
        if btn:
            is_updatable = status == "可更新"
            btn.setEnabled(is_updatable)
            if not is_updatable:
                btn.setStyleSheet("opacity: 0.5;")
            else:
                btn.setStyleSheet("")

    # ====================== 更新逻辑 ======================
    def pull_repo(self, repo: str, row: int):
        logger.info(f"[更新] {repo}")

        if os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")):
            logger.warning("存在未完成 merge，跳过")
            self.load_repo_info(row, repo)
            return

        clean, _, _ = self.run_git(repo, ["status", "--porcelain"])
        if clean.strip():
            logger.warning("工作区有未提交更改，跳过")
            self.load_repo_info(row, repo)
            return

        out, err, code = self.run_git(repo, ["-c", "core.editor=true", "pull", "--ff-only"])

        if code == 0:
            logger.success(f"{repo} 更新成功")
        else:
            logger.error(f"{repo} 更新失败\n{err}")

        # 刷新行（会重新设置按钮状态）
        self.load_repo_info(row, repo)

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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitManager()
    window.show()
    sys.exit(app.exec())
