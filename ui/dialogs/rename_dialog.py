"""重命名仓库对话框"""

import re

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import InfoBar, LineEdit, PushButton

from ui.widgets.dark_window import DarkDialog, apply_tooltip


class RenameRepoDialog(DarkDialog):
    """重命名仓库对话框"""

    def __init__(self, current_name: str, parent=None):
        super().__init__(parent)

        self.current_name = current_name
        self.new_name = None

        self._init_ui()
        self._connect_signal()

    def _init_ui(self):
        """初始化界面"""

        self.setWindowTitle("重命名仓库")

        self.resize(420, 220)
        self.setMinimumWidth(380)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        # 顶部下移避开标题栏
        main_layout.setContentsMargins(16, 44, 16, 16)

        # 当前名称
        current_label = BodyLabel(f"仓库名称：{self.current_name}")

        main_layout.addWidget(current_label)

        # 输入框

        name_label = BodyLabel("新名称:")

        main_layout.addWidget(name_label)

        self.name_input = LineEdit()

        self.name_input.setText(self.current_name)
        self.name_input.selectAll()
        self.name_input.setClearButtonEnabled(True)

        main_layout.addWidget(self.name_input)

        # 提示

        tip = BodyLabel("提示：只会修改本地仓库目录名称，不影响 Git 远程地址")

        tip.setStyleSheet("color:#777;")

        main_layout.addWidget(tip)

        main_layout.addStretch()

        # 按钮

        button_layout = QHBoxLayout()

        button_layout.addStretch()

        self.cancel_btn = PushButton(FIF.CANCEL, "取消")
        apply_tooltip(self.cancel_btn, "取消重命名并关闭窗口")

        self.confirm_btn = PushButton(FIF.SAVE, "确认")
        apply_tooltip(self.confirm_btn, "确认重命名项目文件夹")

        button_layout.addWidget(self.cancel_btn)

        button_layout.addWidget(self.confirm_btn)

        main_layout.addLayout(button_layout)

    def _connect_signal(self):
        """绑定事件"""

        self.cancel_btn.clicked.connect(self.reject)

        self.confirm_btn.clicked.connect(self._on_confirm)

        self.name_input.returnPressed.connect(self._on_confirm)

    def showEvent(self, event):
        """显示后自动聚焦"""

        super().showEvent(event)

        self.name_input.setFocus()

    def _validate_name(self, name: str):
        """
        验证目录名称

        Returns:
            bool
        """

        if not name:
            InfoBar.warning("名称错误", "新名称不能为空", parent=self)

            return False

        if name == self.current_name:

            InfoBar.info("提示", "新名称没有变化", parent=self)

            return False

        # Windows 文件名非法字符

        if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):

            InfoBar.warning("名称错误", "名称包含 Windows 不允许的字符", parent=self)

            return False

        # 点结尾

        if name.endswith("."):

            InfoBar.warning("名称错误", "名称不能以点结尾", parent=self)

            return False

        return True

    def _on_confirm(self):
        """确认"""

        new_name = self.name_input.text().strip()

        if not self._validate_name(new_name):
            return

        self.new_name = new_name

        self.accept()

    def get_new_name(self) -> str:
        """获取新名称"""

        return self.new_name or self.current_name
