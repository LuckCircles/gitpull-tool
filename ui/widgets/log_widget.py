"""日志显示widget"""

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import StrongBodyLabel, TextEdit


class LogWidget(QWidget):
    """日志显示widget"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        layout.addWidget(StrongBodyLabel("运行日志"))

        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

    def append_log(self, message: str):
        """追加日志信息"""
        self.log_text.append(message)

    def clear_log(self):
        """清空日志"""
        self.log_text.clear()
