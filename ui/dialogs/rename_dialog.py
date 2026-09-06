"""重命名仓库对话框 - Fluent 遮罩弹窗"""

import re

from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    LineEdit,
    MessageBoxBase,
    StrongBodyLabel,
)

from ui.widgets.dark_window import MaskFadeGuardMixin, apply_tooltip


class RenameRepoDialog(MaskFadeGuardMixin, MessageBoxBase):
    """重命名仓库对话框（Fluent 遮罩弹窗）"""

    def __init__(self, current_name: str, parent=None):
        super().__init__(parent)

        self.current_name = current_name
        self.new_name = None

        self.widget.setFixedWidth(480)

        self.viewLayout.addWidget(StrongBodyLabel("重命名仓库"))

        # 当前名称
        self.viewLayout.addWidget(BodyLabel(f"仓库名称：{self.current_name}"))

        # 输入框
        self.viewLayout.addWidget(BodyLabel("新名称:"))

        self.name_input = LineEdit()
        self.name_input.setText(self.current_name)
        self.name_input.selectAll()
        self.name_input.setClearButtonEnabled(True)
        # 回车触发主按钮（走 validate 校验）
        self.name_input.returnPressed.connect(self.yesButton.click)
        self.viewLayout.addWidget(self.name_input)

        # 提示
        tip = BodyLabel("提示：只会修改本地仓库目录名称，不影响 Git 远程地址")
        tip.setStyleSheet("color:#777;")
        self.viewLayout.addWidget(tip)

        self.yesButton.setText("确认")
        apply_tooltip(self.yesButton, "确认重命名项目文件夹")
        self.cancelButton.setText("取消")
        apply_tooltip(self.cancelButton, "取消重命名并关闭窗口")

    def showEvent(self, event):
        """显示后自动聚焦输入框。"""
        super().showEvent(event)
        self.name_input.setFocus()

    def _validate_name(self, name: str) -> bool:
        """验证目录名称"""
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

    def validate(self) -> bool:
        """点击确认时校验并记录新名称。"""
        new_name = self.name_input.text().strip()
        if not self._validate_name(new_name):
            return False

        self.new_name = new_name
        return True

    def get_new_name(self) -> str:
        """获取新名称"""
        return self.new_name or self.current_name
