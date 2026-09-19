"""分支管理对话框 — Fluent 遮罩弹窗 + 分支列表线程池加载。

fetch 是网络操作（超时 30s），在主线程执行会冻结整个窗口，
因此加载流程经 TaskExecutor 提交到全局线程池，通过 Signal 回传结果。
"""

import os

from loguru import logger
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)
from qfluentwidgets import (
    IndeterminateProgressBar,
    MessageBoxBase,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)

from core.git_runner import GitRunner
from ui.widgets.dark_window import ConfirmDialog, MaskFadeGuardMixin, apply_tooltip
from utils.qt_executor import QFuture, TaskExecutor


def _run_git(repo: str, args: list[str], timeout: int):
    return GitRunner.run_simple(
        args,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def collect_branches(repo_path: str) -> list[dict]:
    """后台加载分支列表：fetch（网络）+ for-each-ref（本地）解析为行数据。

    线程安全：只执行 git 命令并返回数据，不触碰任何 Qt 控件。
    """
    _run_git(repo_path, ["git", "fetch", "--quiet"], timeout=30)

    current = ""
    r = _run_git(repo_path, ["git", "branch", "--show-current"], timeout=15)
    if r.returncode == 0:
        current = r.stdout.strip()

    result = _run_git(
        repo_path,
        [
            "git",
            "for-each-ref",
            "--format=%(refname)|%(refname:short)|%(objectname:short)|%(committerdate:format:%Y-%m-%d %H:%M)|%(subject)",
            "refs/heads",
            "refs/remotes",
        ],
        timeout=15,
    )
    if result.returncode != 0:
        return []

    rows: list[dict] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            refname, short_name, commit, date, message = line.split("|", 4)
        except ValueError:
            continue
        if refname.startswith("refs/remotes/") and refname.endswith("/HEAD"):
            continue

        is_remote = refname.startswith("refs/remotes/")
        rows.append(
            {
                "name": short_name,
                "refname": refname,
                "is_remote": is_remote,
                "is_current": not is_remote and short_name == current,
                "commit": commit,
                "date": date,
                "message": message,
            }
        )
    return rows


class BranchDialog(MaskFadeGuardMixin, MessageBoxBase):
    """分支管理对话框（Fluent 遮罩弹窗，分支列表线程池加载，UI 不冻结）"""

    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        repo_name = os.path.basename(repo_path.rstrip("\\/"))

        self._load_future: QFuture | None = None

        self.widget.setFixedWidth(880)

        # 遮罩弹窗无标题栏，标题显示在内容区顶部
        self.viewLayout.addWidget(StrongBodyLabel(f"分支管理 · {repo_name}"))

        self.progress = IndeterminateProgressBar()
        self.progress.setVisible(False)
        self.viewLayout.addWidget(self.progress)

        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["分支", "类型", "当前", "最新提交", "提交信息"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setFixedHeight(420)

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        self.viewLayout.addWidget(self.table)

        # 底部按钮：「切换分支」「关闭窗口」均为普通按钮
        # （隐藏自带的主色 yesButton，在按钮区左侧插入普通按钮，等宽分布）
        self.yesButton.hide()
        self.switch_btn = PushButton("切换分支")
        apply_tooltip(self.switch_btn, "切换到选中的分支")
        self.switch_btn.clicked.connect(self.switch_to_branch)
        self.switch_btn.setFocus()
        self.buttonLayout.insertWidget(0, self.switch_btn, 1, Qt.AlignVCenter)

        self.cancelButton.setText("关闭窗口")
        apply_tooltip(self.cancelButton, "关闭分支管理窗口")

        self.load_branches()

    # ------------------------------------------------------------------
    # 后台加载
    # ------------------------------------------------------------------
    def load_branches(self):
        """线程池加载分支列表（fetch 网络操作不阻塞 UI）。"""
        self._set_loading(True)

        self._load_future = (
            TaskExecutor.run(collect_branches, self.repo_path)
            .then(
                on_success=self._on_branches_loaded,
                on_failed=self._on_branches_failed,
            )
        )

    def _set_loading(self, loading: bool):
        self.switch_btn.setEnabled(not loading)
        if loading:
            self.progress.setVisible(True)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.setVisible(False)

    def _on_branches_loaded(self, rows: list[dict]):
        self._set_loading(False)
        self._fill_table(rows)

    def _on_branches_failed(self, message: str):
        self._set_loading(False)
        logger.error(f"加载分支失败: {message}")
        self.table.setRowCount(1)
        self.table.setItem(0, 0, QTableWidgetItem(f"分支加载失败: {message}"))

    def _fill_table(self, rows: list[dict]):
        self.table.setRowCount(0)
        for r in rows:
            branch_item = QTableWidgetItem(r["name"])
            branch_item.setData(
                Qt.UserRole,
                {
                    "name": r["name"],
                    "refname": r["refname"],
                    "is_remote": r["is_remote"],
                },
            )

            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, branch_item)
            self.table.setItem(
                row, 1, QTableWidgetItem("远程" if r["is_remote"] else "本地")
            )
            self.table.setItem(row, 2, QTableWidgetItem("✓" if r["is_current"] else ""))
            self.table.setItem(
                row, 3, QTableWidgetItem(f"{r['commit']}  {r['date']}".strip())
            )
            self.table.setItem(row, 4, QTableWidgetItem(r["message"]))

            if r["is_current"]:
                for col in range(5):
                    item = self.table.item(row, col)
                    if item:
                        item.setBackground(QColor(0, 120, 215, 40))
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)

    def done(self, code):
        """关闭时丢弃仍在执行的加载任务结果（future 由 TaskExecutor 保活至结束）。"""
        if self._load_future is not None:
            self._load_future.detach()
            self._load_future = None
        super().done(code)

    # ------------------------------------------------------------------
    # 分支切换
    # ------------------------------------------------------------------
    def on_double_click(self, item):
        """双击行时切换分支"""
        self.switch_to_branch()

    def switch_to_branch(self):
        """切换到选定的分支"""
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

        box = ConfirmDialog(
            "确认切换分支",
            f"确定要切换到该{branch_type}？\n\n分支: {branch_name}\n\n未提交的更改可能导致切换失败，请先确认工作区状态。",
            self,
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_branch(self.repo_path, branch_info, self)
