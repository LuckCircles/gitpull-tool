"""删除仓库对话框 - Fluent 风格"""

import os

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    InfoBar,
    PushButton,
    RadioButton,
    StrongBodyLabel,
)

from ui.widgets.dark_window import DarkDialog, apply_tooltip


class DeleteRepoDialog(DarkDialog):
    """
    删除仓库对话框

    支持:
        1. 删除整个仓库目录
        2. 仅删除 .git 文件夹
    """

    DELETE_ALL = 0
    DELETE_GIT_ONLY = 1

    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)

        self.repo_path = os.path.abspath(repo_path)

        self.repo_name = os.path.basename(self.repo_path.rstrip("\\/"))

        self.delete_mode = self.DELETE_ALL

        self._init_ui()
        self._connect_signal()

    def _init_ui(self):

        self.setWindowTitle("删除仓库")

        self.resize(450, 320)

        layout = QVBoxLayout(self)

        layout.setSpacing(12)

        # 顶部下移避开标题栏

        layout.setContentsMargins(16, 44, 16, 16)

        # 标题

        # title = StrongBodyLabel("确认删除仓库")

        # layout.addWidget(title)

        # 仓库信息

        info = BodyLabel(f"""
仓库名称:
{self.repo_name}

路径:
{self.repo_path}
""")

        info.setWordWrap(True)

        layout.addWidget(info)

        # 删除模式标题

        mode_title = StrongBodyLabel("删除方式")

        layout.addWidget(mode_title)

        # Fluent RadioButton

        self.full_delete_radio = RadioButton("完全删除")

        self.full_delete_radio.setChecked(True)

        desc1 = BodyLabel("删除整个仓库目录，包括所有代码文件")

        desc1.setStyleSheet("color:#888;")

        layout.addWidget(self.full_delete_radio)

        layout.addWidget(desc1)

        self.git_only_radio = RadioButton("仅删除 Git 信息")

        desc2 = BodyLabel("保留代码文件，后续不会继续扫描仓库")

        desc2.setStyleSheet("color:#888;")

        layout.addWidget(self.git_only_radio)

        layout.addWidget(desc2)

        # 警告

        warning = BodyLabel("⚠ 此操作不可恢复，请确认后继续")

        warning.setStyleSheet("""
            color:#d13438;
            """)

        layout.addWidget(warning)

        layout.addStretch()

        # 按钮

        button_layout = QHBoxLayout()

        button_layout.addStretch()

        self.cancel_btn = PushButton(FIF.CANCEL, "取消")
        apply_tooltip(self.cancel_btn, "取消删除并关闭窗口")

        self.confirm_btn = PushButton(FIF.DELETE, "确认")
        apply_tooltip(self.confirm_btn, "确认按所选方式删除仓库（不可恢复）")

        button_layout.addWidget(self.cancel_btn)

        button_layout.addWidget(self.confirm_btn)

        layout.addLayout(button_layout)

    def _connect_signal(self):

        self.cancel_btn.clicked.connect(self.reject)

        self.confirm_btn.clicked.connect(self._on_confirm)

    def _on_confirm(self):

        if not self.repo_path:

            InfoBar.error("错误", "仓库路径为空", parent=self)

            return

        if self.full_delete_radio.isChecked():

            self.delete_mode = self.DELETE_ALL

        else:

            self.delete_mode = self.DELETE_GIT_ONLY

        self.accept()

    def get_delete_mode(self):

        return self.delete_mode

    def is_delete_all(self):

        return self.delete_mode == self.DELETE_ALL

    def is_delete_git_only(self):

        return self.delete_mode == self.DELETE_GIT_ONLY
