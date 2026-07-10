"""删除仓库对话框 - 支持多种删除模式"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QRadioButton,
    QVBoxLayout,
)
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton, StrongBodyLabel


class DeleteRepoDialog(QDialog):
    """删除仓库对话框，支持"完全删除"和"仅删除Git"两种模式"""

    DELETE_ALL = 0  # 删除整个目录
    DELETE_GIT_ONLY = 1  # 仅删除.git文件夹

    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.repo_name = os.path.basename(repo_path.rstrip("\\/"))
        self.delete_mode = self.DELETE_ALL  # 默认为完全删除
        self._init_ui()

    def _init_ui(self):
        """初始化界面"""
        self.setWindowTitle("删除仓库")
        self.setWindowModality(Qt.WindowModal)

        # 创建主布局
        layout = QVBoxLayout()

        # 标题和说明
        title = StrongBodyLabel("确认删除仓库")
        layout.addWidget(title)

        # 显示仓库信息
        info_text = f"仓库名称: {self.repo_name}\n路径: {self.repo_path}"
        info_label = BodyLabel(info_text)
        layout.addWidget(info_label)

        layout.addSpacing(15)

        # 删除模式选择
        mode_group = QGroupBox("选择删除模式")
        mode_layout = QVBoxLayout()

        self.mode_buttons = QButtonGroup()

        # 完全删除选项
        full_delete_btn = QRadioButton("完全删除\n删除整个仓库目录及所有文件")
        full_delete_btn.setChecked(True)
        self.mode_buttons.addButton(full_delete_btn, self.DELETE_ALL)
        mode_layout.addWidget(full_delete_btn)

        # 仅删除Git选项
        git_only_btn = QRadioButton("仅删除Git\n保留所有文件，仅删除.git文件夹")
        self.mode_buttons.addButton(git_only_btn, self.DELETE_GIT_ONLY)
        mode_layout.addWidget(git_only_btn)

        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        layout.addSpacing(10)

        # 警告信息
        warning_label = BodyLabel("⚠️ 此操作不可撤销，请谨慎选择")
        warning_label.setStyleSheet("color: #ff6b6b;")
        layout.addWidget(warning_label)

        layout.addStretch()

        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        cancel_btn = PushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        confirm_btn = PrimaryPushButton("确认删除")
        confirm_btn.clicked.connect(self._on_confirm)
        button_layout.addWidget(confirm_btn)

        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.setMinimumWidth(400)

    def _on_confirm(self):
        """确认删除时更新模式并关闭对话框"""
        self.delete_mode = self.mode_buttons.checkedId()
        self.accept()

    def get_delete_mode(self) -> int:
        """获取选择的删除模式"""
        return self.delete_mode

    def is_delete_all(self) -> bool:
        """是否完全删除"""
        return self.delete_mode == self.DELETE_ALL

    def is_delete_git_only(self) -> bool:
        """是否仅删除Git"""
        return self.delete_mode == self.DELETE_GIT_ONLY
