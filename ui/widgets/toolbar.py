"""仓库页面工具栏widget"""

from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    StrongBodyLabel,
)


class RepoToolbar(QWidget):
    """仓库页面工具栏"""

    def __init__(self, base_dir: str, parent=None):
        super().__init__(parent)
        self._init_ui(base_dir)

    def _init_ui(self, base_dir: str):
        """初始化工具栏"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 目录标签
        self.dir_label = StrongBodyLabel(f"目录: {base_dir}")
        layout.addWidget(self.dir_label)
        layout.addStretch()

        # 搜索框
        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText("搜索仓库...")
        self.search_edit.setFixedWidth(200)
        self.search_edit.setClearButtonEnabled(True)
        layout.addWidget(self.search_edit)

        # 按钮
        self.scan_btn = PushButton(FIF.SYNC, "扫描仓库")
        self.add_repo_btn = PushButton(FIF.ADD, "添加仓库")
        self.remove_repo_btn = PushButton(FIF.DELETE, "删除仓库")
        self.bulk_update_btn = PrimaryPushButton(FIF.UPDATE, "一键更新")

        layout.addWidget(self.scan_btn)
        layout.addWidget(self.add_repo_btn)
        layout.addWidget(self.remove_repo_btn)
        layout.addWidget(self.bulk_update_btn)

    def update_dir_label(self, base_dir: str):
        """更新目录标签"""
        self.dir_label.setText(f"目录: {base_dir}")
