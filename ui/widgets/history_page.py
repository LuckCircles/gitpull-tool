"""版本历史弹出窗口 — SegmentedWidget 本地/远端历史导航（Fluent 遮罩弹窗）。

结构：
    HistoryWindow (MessageBoxBase 遮罩弹窗)
      ├── 标题（版本历史 · 仓库名）
      ├── SegmentedWidget（"本地历史" / "远端历史" 切换，胶囊滑块动画）
      └── QStackedWidget
            ├── _CommitList(remote=False)  → git log HEAD
            └── _CommitList(remote=True)   → git log origin/<当前分支>

加载流程（线程池）：
    git 命令序列经 TaskExecutor 提交到全局线程池，结果经 Signal 回传；
    快速切换仓库时按代数（generation）丢弃过期结果。

本地/远端区分：
    - 每页标题显示分支信息与同步状态（未推送 N 个 / 未拉取 N 个 / 与远端一致）
    - 仅存在于一侧的提交以橙色高亮：本地页=尚未推送，远端页=尚未拉取
    - 当前 HEAD 以蓝色加粗标识
"""

import os

from loguru import logger
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
    MessageBoxBase,
    PushButton,
    SegmentedWidget,
    StrongBodyLabel,
    TableWidget,
)

from core.git_runner import GitRunner
from ui.widgets.dark_window import ConfirmDialog, MaskFadeGuardMixin, apply_tooltip
from utils.qt_executor import QFuture, TaskExecutor

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
        self.table.setFixedHeight(430)
        self.table.itemDoubleClicked.connect(lambda _: self.switch_to_version())
        layout.addWidget(self.table, 1)

        btn_layout = QHBoxLayout()
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color:#ff9800;")
        btn_layout.addWidget(self.hint_label)
        btn_layout.addStretch()
        # 两个按钮均分剩余宽度，铺满整行
        self.switch_btn = PushButton(FIF.UPDATE, "切换版本")
        apply_tooltip(self.switch_btn, "硬重置到选中的提交（丢弃未提交更改）")
        self.switch_btn.clicked.connect(self.switch_to_version)
        btn_layout.addWidget(self.switch_btn, 1)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        apply_tooltip(self.close_btn, "关闭版本历史窗口")
        self.close_btn.clicked.connect(self._page.reject)
        btn_layout.addWidget(self.close_btn, 1)
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


def _git(repo: str, args: list[str], timeout: int):
    return GitRunner.run_simple(
        args,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def collect_history(repo_path: str) -> dict:
    """后台加载版本历史：当前分支 + 本地/远端提交列表。

    git 命令序列经 TaskExecutor 在线程池中执行（大仓库可能较慢），
    线程安全：只执行 git 命令并返回数据，不触碰任何 Qt 控件。
    """
    branch_result = _git(repo_path, ["git", "branch", "--show-current"], timeout=15)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    local_commits = _git_log(repo_path, ["HEAD"])

    remote_ref = f"origin/{branch}" if branch else ""
    remote_exists = False
    remote_commits: list[dict] = []
    if remote_ref:
        check = _git(
            repo_path,
            ["git", "rev-parse", "--verify", "--quiet", remote_ref],
            timeout=15,
        )
        remote_exists = check.returncode == 0
        if remote_exists:
            remote_commits = _git_log(repo_path, [remote_ref])
    return {
        "branch": branch,
        "local_commits": local_commits,
        "remote_exists": remote_exists,
        "remote_commits": remote_commits,
    }


def _git_log(repo_path: str, target: list[str]) -> list[dict]:
    """执行 git log 并解析为提交列表。"""
    cmd = [
        "git",
        "log",
        *target,
        "--pretty=format:%H|%ad|%an|%s",
        "--date=format:%Y-%m-%d %H:%M",
        f"-{LOG_LIMIT}",
    ]
    result = _git(repo_path, cmd, timeout=20)

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


class HistoryWindow(MaskFadeGuardMixin, MessageBoxBase):
    """版本历史弹出窗口（Fluent 遮罩弹窗），内含 SegmentedWidget 本地/远端导航。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.widget.setFixedWidth(960)

        self._manager = manager
        self.repo_path = ""

        # 后台加载：代数计数（快速切换仓库时丢弃过期结果）+ 当前加载任务
        self._load_generation = 0
        self._load_future: QFuture | None = None

        # 遮罩弹窗无标题栏，标题显示在内容区顶部
        self.title_label = StrongBodyLabel("版本历史")
        self.viewLayout.addWidget(self.title_label)

        self.pivot = SegmentedWidget(self)
        self.stacked = QStackedWidget(self)

        self.local_list = _CommitList(remote=False, page=self)
        self.remote_list = _CommitList(remote=True, page=self)

        self._add_sub_interface(self.local_list, "local-history", "本地历史")
        self._add_sub_interface(self.remote_list, "remote-history", "远端历史")

        self.viewLayout.addWidget(self.pivot)
        self.viewLayout.addWidget(self.stacked)

        self.stacked.setCurrentWidget(self.local_list)
        self.pivot.setCurrentItem("local-history")

        # 底部按钮区整体隐藏：「切换版本」与「关闭」按钮在各列表底部同一行
        self.buttonGroup.hide()

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
    # 数据加载（线程池）
    # ------------------------------------------------------------------
    def load_repo(self, repo_path: str):
        """线程池加载指定仓库的本地/远端历史，并计算两侧差异。"""
        self.repo_path = os.path.abspath(repo_path)
        name = os.path.basename(self.repo_path.rstrip("\\/"))
        self.title_label.setText(f"版本历史 · {name}")
        logger.info(f"[历史] 加载 {self.repo_path}")

        # 代数计数：窗口复用时快速切换仓库，旧任务结果按代数丢弃
        self._load_generation += 1
        generation = self._load_generation
        self.local_list.set_loading()
        self.remote_list.set_loading()

        def on_loaded(data: dict):
            if generation == self._load_generation:
                self._apply_loaded(data)

        def on_failed(message: str):
            if generation == self._load_generation:
                logger.error(f"[历史] 加载失败 {self.repo_path}: {message}")
                self.local_list.set_error(f"加载失败: {message}")
                self.remote_list.set_error("")

        self._load_future = (
            TaskExecutor.run(collect_history, self.repo_path)
            .then(on_success=on_loaded, on_failed=on_failed)
        )

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
            remote_info = (
                f"远端分支 {remote_ref or 'origin/未知'} 不存在（可能未推送或未 fetch）"
            )
            local_hint = ""
            remote_hint = ""

        self.local_list.set_data(local_commits, local_only, local_info, local_hint)
        self.remote_list.set_data(remote_commits, remote_only, remote_info, remote_hint)

    def done(self, code):
        """关闭时丢弃仍在执行的加载任务结果（future 由 TaskExecutor 保活至结束）。"""
        if self._load_future is not None:
            self._load_future.detach()
            self._load_future = None
        super().done(code)

    def reset_to_commit(self, commit: str):
        """硬重置到指定提交后刷新历史。"""
        if not self.repo_path:
            return
        self._manager.switch_to_commit(self.repo_path, commit)
        self.load_repo(self.repo_path)
