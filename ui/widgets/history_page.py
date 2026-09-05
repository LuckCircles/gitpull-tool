"""版本历史独立窗口 — SegmentedWidget 本地/远端历史导航（QWidget 弹出窗口，非对话框）。

结构：
    HistoryWindow (QWidget, 独立弹出窗口，原生标题栏)
      ├── SegmentedWidget（"本地历史" / "远端历史" 切换，胶囊滑块动画）
      └── QStackedWidget
            ├── _CommitList(remote=False)  → git log HEAD
            └── _CommitList(remote=True)   → git log origin/<当前分支>

加载流程（后台线程）：
    git 命令序列在 HistoryWorker（QThread）中执行，经 Signal 回传；
    快速切换仓库时按代数（generation）丢弃过期结果。

本地/远端区分：
    - 每页标题显示分支信息与同步状态（未推送 N 个 / 未拉取 N 个 / 与远端一致）
    - 仅存在于一侧的提交以橙色高亮：本地页=尚未推送，远端页=尚未拉取
    - 当前 HEAD 以蓝色加粗标识
"""

import os

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    PrimaryPushButton,
    SegmentedWidget,
    StrongBodyLabel,
    TableWidget,
)

from core.git_runner import GitRunner
from ui.widgets.dark_window import ConfirmDialog, DarkDialog, apply_tooltip

LOG_LIMIT = 50

# 蓝色：当前 HEAD 所在行
CURRENT_COLOR = QColor(0, 120, 215, 40)
# 橙色：仅存在于一侧的差异提交
DIVERGED_COLOR = QColor(255, 152, 0, 46)


class _CommitList(QWidget):
    """提交列表视图（本地 / 远端共用）。"""

    def __init__(self, remote: bool, page, parent=None):
        super().__init__(parent)
        self.remote = remote
        self._page = page
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        self.info_label = StrongBodyLabel("尚未加载仓库")
        layout.addWidget(self.info_label)

        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Commit", "日期", "作者", "提交信息"])
        self.table.verticalHeader().setVisible(False)

        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(lambda _: self.switch_to_version())
        layout.addWidget(self.table, 1)

        btn_layout = QHBoxLayout()
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color:#ff9800;")
        btn_layout.addWidget(self.hint_label)
        btn_layout.addStretch()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换版本")
        apply_tooltip(self.switch_btn, "硬重置到选中的提交（丢弃未提交更改）")
        self.switch_btn.clicked.connect(self.switch_to_version)
        btn_layout.addWidget(self.switch_btn)
        layout.addLayout(btn_layout)

    def set_loading(self):
        """加载中占位状态（后台线程执行 git 命令期间）。"""
        self.info_label.setText("正在加载提交历史...")
        self.hint_label.setText("")
        self.table.setRowCount(0)

    def set_error(self, message: str):
        """加载失败状态。"""
        self.info_label.setText(message or "加载失败")
        self.hint_label.setText("")
        self.table.setRowCount(0)

    def set_data(
        self,
        commits: list[dict],
        diverged: set[str],
        info_text: str,
        hint_text: str,
    ):
        """填充提交列表并高亮差异行。"""
        self.info_label.setText(info_text)
        self.hint_label.setText(hint_text)

        self.table.setRowCount(0)
        for i, c in enumerate(commits):
            item_commit = QTableWidgetItem(c["short"])
            item_commit.setData(Qt.UserRole, c["hash"])
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, item_commit)
            self.table.setItem(row, 1, QTableWidgetItem(c["date"]))
            self.table.setItem(row, 2, QTableWidgetItem(c["author"]))
            self.table.setItem(row, 3, QTableWidgetItem(c["message"]))

            is_head = (not self.remote) and i == 0
            is_diverged = c["hash"] in diverged
            if is_head or is_diverged:
                for col in range(4):
                    item = self.table.item(row, col)
                    if item:
                        item.setBackground(CURRENT_COLOR if is_head else DIVERGED_COLOR)
                        font = item.font()
                        font.setBold(is_head)
                        item.setFont(font)

    def switch_to_version(self):
        """切换到选定的版本（硬重置，需确认）。"""
        selected = self.table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        item = self.table.item(row, 0)
        if not item:
            return

        # 优先使用完整 hash
        commit = item.data(Qt.UserRole) or item.text()

        box = ConfirmDialog(
            "确认切换版本",
            f"确定要切换到该版本？\n\nCommit: {commit[:12]}\n\n⚠ 此操作会丢弃所有未提交更改！",
            self._page,
        )
        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self._page.reset_to_commit(commit)


class HistoryWorker(QObject):
    """后台加载版本历史：当前分支 + 本地/远端提交列表。

    git 命令序列在 QThread 中执行（大仓库可能较慢），
    结果经 Signal 回传主线程。
    """

    loaded = Signal(dict)  # branch, local_commits, remote_exists, remote_commits
    failed = Signal(str)

    def __init__(self, repo_path: str):
        super().__init__()
        self._repo = repo_path

    def run(self):
        try:
            self.loaded.emit(self._collect())
        except Exception as e:  # noqa: BLE001 后台线程兜底，错误经信号回传
            self.failed.emit(str(e))

    def _collect(self) -> dict:
        branch = self._current_branch()
        local_commits = self._log(["HEAD"])

        remote_ref = f"origin/{branch}" if branch else ""
        remote_exists = False
        remote_commits: list[dict] = []
        if remote_ref:
            check = GitRunner.run_simple(
                ["git", "rev-parse", "--verify", "--quiet", remote_ref],
                cwd=self._repo,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            remote_exists = check.returncode == 0
            if remote_exists:
                remote_commits = self._log([remote_ref])
        return {
            "branch": branch,
            "local_commits": local_commits,
            "remote_exists": remote_exists,
            "remote_commits": remote_commits,
        }

    def _current_branch(self) -> str:
        result = GitRunner.run_simple(
            ["git", "branch", "--show-current"],
            cwd=self._repo,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _log(self, target: list[str]) -> list[dict]:
        """执行 git log 并解析为提交列表。"""
        cmd = [
            "git",
            "log",
            *target,
            "--pretty=format:%H|%ad|%an|%s",
            "--date=format:%Y-%m-%d %H:%M",
            f"-{LOG_LIMIT}",
        ]
        result = GitRunner.run_simple(
            cmd,
            cwd=self._repo,
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )

        commits: list[dict] = []
        if result.returncode != 0:
            return commits

        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                h, date, author, msg = line.split("|", 3)
            except ValueError:
                continue
            commits.append(
                {
                    "hash": h,
                    "short": h[:12],
                    "date": date,
                    "author": author,
                    "message": msg,
                }
            )
        return commits


class HistoryWindow(DarkDialog):
    """版本历史独立弹出窗口（QDialog 体系暗色无边框），内含 SegmentedWidget 本地/远端导航。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        # 独立顶层窗口，图标 + 标题由 DarkDialog 统一提供
        self.setWindowTitle("版本历史")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(980, 680)

        self._manager = manager
        self.repo_path = ""

        # 后台加载：代数计数（快速切换仓库时丢弃过期结果）+ 线程引用
        self._load_generation = 0
        self._load_threads: list[tuple[QThread, HistoryWorker]] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        # 顶部下移 48px 避开标题栏
        layout.setContentsMargins(16, 60, 16, 16)

        self.repo_label = StrongBodyLabel("在仓库页点击「当前版本」列查看对应仓库历史")
        layout.addWidget(self.repo_label)

        self.pivot = SegmentedWidget(self)
        self.stacked = QStackedWidget(self)

        self.local_list = _CommitList(remote=False, page=self)
        self.remote_list = _CommitList(remote=True, page=self)

        self._add_sub_interface(self.local_list, "local-history", "本地历史")
        self._add_sub_interface(self.remote_list, "remote-history", "远端历史")

        layout.addWidget(self.pivot)
        layout.addWidget(self.stacked, 1)

        self.stacked.setCurrentWidget(self.local_list)
        self.pivot.setCurrentItem("local-history")

    def _add_sub_interface(self, widget: QWidget, key: str, text: str):
        widget.setObjectName(key)
        self.stacked.addWidget(widget)
        # 注意：SegmentedWidget 的 itemClicked 是 Signal(bool)，
        # onClick 必须是无参 lambda（或接受一个 bool），否则会收到 bool 参数
        self.pivot.addItem(
            routeKey=key,
            text=text,
            onClick=lambda: self.stacked.setCurrentWidget(widget),
        )

    # ------------------------------------------------------------------
    # 数据加载（后台线程）
    # ------------------------------------------------------------------
    def load_repo(self, repo_path: str):
        """后台线程加载指定仓库的本地/远端历史，并计算两侧差异。"""
        self.repo_path = os.path.abspath(repo_path)
        name = os.path.basename(self.repo_path.rstrip("\\/"))
        self.setWindowTitle(f"版本历史 · {name}")
        self.repo_label.setText(f"版本历史 · {name}")
        logger.info(f"[历史] 加载 {self.repo_path}")

        # 代数计数：窗口复用时快速切换仓库，旧线程结果按代数丢弃
        self._load_generation += 1
        generation = self._load_generation
        self.local_list.set_loading()
        self.remote_list.set_loading()

        worker = HistoryWorker(self.repo_path)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def on_loaded(data: dict):
            self._retire_load_thread(thread)
            if generation == self._load_generation:
                self._apply_loaded(data)

        def on_failed(message: str):
            self._retire_load_thread(thread)
            if generation == self._load_generation:
                logger.error(f"[历史] 加载失败 {self.repo_path}: {message}")
                self.local_list.set_error(f"加载失败: {message}")
                self.remote_list.set_error("")

        worker.loaded.connect(on_loaded)
        worker.failed.connect(on_failed)

        # 持有线程引用直至结束，防止运行中被 GC
        self._load_threads.append((thread, worker))
        thread.finished.connect(lambda: self._discard_load_thread(thread))
        thread.start()

    def _retire_load_thread(self, thread: QThread):
        """结果已回传，请求线程事件循环退出。"""
        thread.quit()

    def _discard_load_thread(self, thread: QThread):
        for entry in self._load_threads:
            if entry[0] is thread:
                self._load_threads.remove(entry)
                break

    def _apply_loaded(self, data: dict):
        """在主线程应用加载结果：计算两侧差异并填充列表。"""
        branch = data["branch"]
        local_commits = data["local_commits"]
        remote_exists = data["remote_exists"]
        remote_commits = data["remote_commits"]
        remote_ref = f"origin/{branch}" if branch else ""

        # 差集：仅存在于一侧的提交
        local_set = {c["hash"] for c in local_commits}
        remote_set = {c["hash"] for c in remote_commits}
        local_only = local_set - remote_set
        remote_only = remote_set - local_set

        if remote_exists:
            ahead, behind = len(local_only), len(remote_only)
            if ahead == 0 and behind == 0:
                local_info = f"本地 · 当前分支: {branch} · 与远端一致"
                remote_info = f"远端 · {remote_ref} · 与本地一致"
            else:
                local_info = f"本地 · 当前分支: {branch} · 未推送 {ahead} 个提交"
                remote_info = f"远端 · {remote_ref} · 未拉取 {behind} 个提交"
            local_hint = "橙色行 = 尚未推送到远端的提交" if ahead else ""
            remote_hint = "橙色行 = 本地尚未包含的提交" if behind else ""
        else:
            local_info = f"本地 · 当前分支: {branch or '游离 HEAD'} · 无对应远端分支"
            remote_info = f"远端分支 {remote_ref or 'origin/未知'} 不存在（可能未推送或未 fetch）"
            local_hint = ""
            remote_hint = ""

        self.local_list.set_data(local_commits, local_only, local_info, local_hint)
        self.remote_list.set_data(remote_commits, remote_only, remote_info, remote_hint)

    def closeEvent(self, event):
        """关闭窗口时停止仍在运行的加载线程。"""
        for thread, worker in list(self._load_threads):
            for sig in (worker.loaded, worker.failed):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            thread.quit()
        super().closeEvent(event)

    def reset_to_commit(self, commit: str):
        """硬重置到指定提交后刷新历史。"""
        if not self.repo_path:
            return
        self._manager.switch_to_commit(self.repo_path, commit)
        self.load_repo(self.repo_path)
