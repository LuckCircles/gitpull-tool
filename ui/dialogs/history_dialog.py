"""版本历史对话框"""

import os

from loguru import logger
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    MessageBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)

from core.git_runner import GitRunner


class HistoryDialog(QDialog):
    """仓库版本历史对话框"""

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

        from PySide6.QtWidgets import QHeaderView

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
        """加载版本历史"""
        try:
            cmd = [
                "git",
                "log",
                "--pretty=format:%H|%ad|%an|%s",
                "--date=format:%Y-%m-%d %H:%M",
                "-30",
            ]

            result = GitRunner.run_simple(
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
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                commit, date, author, message = line.split("|", 3)

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
        """双击行时切换版本"""
        self.switch_to_version()

    def switch_to_version(self):
        """切换到选定的版本"""
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
            "确认切换版本",
            f"确定要切换到该版本？\n\nCommit: {commit[:12]}\n\n⚠ 此操作会丢弃所有未提交更改！",
            self,
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_commit(self.repo_path, commit, self)
