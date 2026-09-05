"""分支管理对话框 — 分支列表后台线程加载。

fetch 是网络操作（超时 30s），在主线程执行会冻结整个窗口，
因此加载流程放入 QThread，通过 Signal/Slot 回传结果。
"""

import os

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    IndeterminateProgressBar,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)

from core.git_runner import GitRunner
from ui.widgets.dark_window import ConfirmDialog, DarkDialog, apply_tooltip


class BranchWorker(QObject):
    """后台加载分支列表：fetch（网络）+ for-each-ref（本地）解析为行数据。"""

    loaded = Signal(list)  # list[dict]
    failed = Signal(str)

    def __init__(self, repo_path: str):
        super().__init__()
        self._repo = repo_path

    def run(self):
        try:
            self.loaded.emit(self._collect())
        except Exception as e:  # noqa: BLE001 后台线程兜底，错误经信号回传
            self.failed.emit(str(e))

    def _collect(self) -> list[dict]:
        self._run(["git", "fetch", "--quiet"], timeout=30)

        current = ""
        r = self._run(["git", "branch", "--show-current"], timeout=15)
        if r.returncode == 0:
            current = r.stdout.strip()

        result = self._run(
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

    def _run(self, args: list[str], timeout: int):
        return GitRunner.run_simple(
            args,
            cwd=self._repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )


class BranchDialog(DarkDialog):
    """分支管理对话框（分支列表后台线程加载，UI 不冻结）"""

    # 类级引用：对话框销毁后仍在执行的加载线程不被 GC
    #（对话框在 main.py 中以临时对象方式创建，exec 返回即被回收）
    _active_workers: set = set()

    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.setWindowTitle(f"分支管理 - {os.path.basename(repo_path)}")
        self.resize(860, 560)

        self._thread: QThread | None = None
        self._worker: BranchWorker | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 44, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        self.progress = IndeterminateProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["分支", "类型", "当前", "最新提交", "提交信息"]
        )
        self.table.verticalHeader().setVisible(False)

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
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换分支")
        apply_tooltip(self.switch_btn, "切换到选中的分支")
        self.switch_btn.clicked.connect(self.switch_to_branch)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        apply_tooltip(self.close_btn, "关闭分支管理窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_branches()

    # ------------------------------------------------------------------
    # 后台加载
    # ------------------------------------------------------------------
    def load_branches(self):
        """后台线程加载分支列表（fetch 网络操作不阻塞 UI）。"""
        self._set_loading(True)

        self._worker = BranchWorker(self.repo_path)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.loaded.connect(self._on_branches_loaded)
        self._worker.failed.connect(self._on_branches_failed)

        key = (self._thread, self._worker)
        BranchDialog._active_workers.add(key)
        self._thread.finished.connect(
            lambda: BranchDialog._active_workers.discard(key)
        )
        self._thread.start()

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

    def closeEvent(self, event):
        """关闭时停止加载线程（引用由 _active_workers 持有直至结束）。"""
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        if thread:
            if worker:
                for sig in (worker.loaded, worker.failed):
                    try:
                        sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            thread.quit()
        super().closeEvent(event)

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
