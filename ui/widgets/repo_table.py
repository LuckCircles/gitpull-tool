"""仓库列表表格widget"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem
from qfluentwidgets import TableWidget


class RepoTable(TableWidget):
    """仓库列表表格"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_table()

    def _init_table(self):
        """初始化表格"""
        self.setColumnCount(7)
        self.setHorizontalHeaderLabels(
            ["", "仓库名称", "当前分支", "当前版本", "最新版本", "状态 / 同步", "操作"]
        )

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)

        self.setColumnWidth(0, 36)
        self.setColumnWidth(2, 140)
        self.setColumnWidth(3, 120)
        self.setColumnWidth(4, 120)
        self.setColumnWidth(5, 170)
        self.setColumnWidth(6, 90)

        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def update_row(
        self,
        row: int,
        repo: str,
        branch: str,
        local: str,
        remote_commit: str,
        status: str,
        ahead_behind: str,
        remote_url: str = "",
    ):
        """更新表格行信息"""
        if row >= self.rowCount():
            return

        old_check_item = self.item(row, 0)
        old_check_state = (
            old_check_item.checkState() if old_check_item else Qt.Unchecked
        )

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
        self.setItem(row, 0, check_item)

        import os

        repo_name = os.path.basename(repo.rstrip("\\/")) or repo
        repo_item = QTableWidgetItem(repo_name)
        repo_item.setData(Qt.UserRole, repo)
        repo_item.setToolTip(repo)
        self.setItem(row, 1, repo_item)

        status_text = (
            f"{status}  {ahead_behind}"
            if ahead_behind not in ("N/A", "✓", "-")
            else status
        )
        for col, text in enumerate([branch, local, remote_commit, status_text]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, col + 2, item)

        # 状态颜色
        status_item = self.item(row, 5)
        if status == "可更新":
            status_item.setForeground(QColor("#ff9800"))
        elif status == "✓ 已同步":
            status_item.setForeground(QColor("#00c853"))
        elif is_ignored:
            status_item.setForeground(QColor("#9e9e9e"))
