"""分支管理对话框"""

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


class BranchDialog(QDialog):
    """分支管理对话框"""

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
        self.table.setHorizontalHeaderLabels(
            ["分支", "类型", "当前", "最新提交", "提交信息"]
        )
        self.table.verticalHeader().setVisible(False)

        from PySide6.QtWidgets import QHeaderView

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
        self.switch_btn.clicked.connect(self.switch_to_branch)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_branches()

    def load_branches(self):
        """加载分支列表"""
        try:
            GitRunner.run_simple(
                ["git", "fetch", "--quiet"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            current_result = GitRunner.run_simple(
                ["git", "branch", "--show-current"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            current_branch = (
                current_result.stdout.strip() if current_result.returncode == 0 else ""
            )

            result = GitRunner.run_simple(
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
                self.table.setItem(
                    row, 3, QTableWidgetItem(f"{commit}  {date}".strip())
                )
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
