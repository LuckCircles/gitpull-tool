"""重命名仓库对话框"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QVBoxLayout
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton, StrongBodyLabel


class RenameRepoDialog(QDialog):
    """重命名仓库对话框"""

    def __init__(self, current_name: str, parent=None):
        super().__init__(parent)
        self.current_name = current_name
        self.new_name = None
        self._init_ui()

    def _init_ui(self):
        """初始化界面"""
        self.setWindowTitle("重命名仓库")
        self.setWindowModality(Qt.WindowModal)

        # 创建主布局
        layout = QVBoxLayout()

        # 标题
        title = StrongBodyLabel("重命名仓库")
        layout.addWidget(title)

        # 当前名称
        current_label = BodyLabel(f"当前名称: {self.current_name}")
        layout.addWidget(current_label)

        layout.addSpacing(10)

        # 输入框
        new_name_label = BodyLabel("新名称:")
        layout.addWidget(new_name_label)

        self.name_input = QLineEdit()
        self.name_input.setText(self.current_name)
        self.name_input.selectAll()
        self.name_input.setMinimumHeight(35)
        layout.addWidget(self.name_input)

        layout.addSpacing(10)

        # 提示
        tip_label = BodyLabel("ℹ️ 仓库目录将被重命名（本地操作）")
        tip_label.setStyleSheet("color: #666666;")
        layout.addWidget(tip_label)

        layout.addStretch()

        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        cancel_btn = PushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        confirm_btn = PrimaryPushButton("确认重命名")
        confirm_btn.clicked.connect(self._on_confirm)
        button_layout.addWidget(confirm_btn)

        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.setMinimumWidth(350)

    def _on_confirm(self):
        """确认重命名"""
        new_name = self.name_input.text().strip()

        # 验证新名称
        if not new_name:
            from qfluentwidgets import InfoBar

            InfoBar.warning("提示", "新名称不能为空", parent=self)
            return

        if new_name == self.current_name:
            from qfluentwidgets import InfoBar

            InfoBar.info("提示", "新名称与当前名称相同", parent=self)
            return

        if "/" in new_name or "\\" in new_name or ":" in new_name:
            from qfluentwidgets import InfoBar

            InfoBar.warning("提示", "新名称不能包含非法字符", parent=self)
            return

        self.new_name = new_name
        self.accept()

    def get_new_name(self) -> str:
        """获取新名称"""
        return self.new_name or self.current_name
