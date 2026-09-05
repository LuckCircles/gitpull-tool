"""暗色无边框对话框基类与通用 UI 助手。

- DarkDialog / ConfirmDialog：qframelesswindow.FramelessDialog（独立窗口 + DarkTitleBar）
- InputDialog：qfluentwidgets.MessageBoxBase（Fluent 风格遮罩弹窗，自动适配明暗主题）
- MaskFadeGuardMixin：修复 MaskDialogBase 淡出动画期间重复 done() 的 painter 冲突
- apply_tooltip：Fluent 风格工具提示助手（悬停 300ms 显示）

主窗口使用 qfluentwidgets.MSFluentWindow，不在此文件。
"""

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    isDarkTheme,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
)
from qfluentwidgets.common.config import qconfig
from qframelesswindow import FramelessDialog, TitleBar

# 暗色主题标准背景色（与官方 FluentWindow 暗色背景一致）
DARK_BG = "rgb(32, 32, 32)"


def apply_tooltip(
    widget,
    text: str,
    position=ToolTipPosition.TOP,
    duration: int = 1000,
):
    """为控件添加 Fluent 风格工具提示。

    鼠标悬停 300ms 后在指定方位显示提示，duration 毫秒后自动隐藏
    （duration < 0 时不自动隐藏）。

    用法::

        apply_tooltip(button, "更新仓库")
    """
    widget.setToolTip(text)
    widget.setToolTipDuration(max(duration, -1))
    widget.installEventFilter(
        ToolTipFilter(widget, showDelay=300, position=position)
    )


class MaskFadeGuardMixin:
    """修复 MaskDialogBase 淡出动画期间的重复 done() 问题。

    qfluentwidgets 的 MaskDialogBase.done() 每次调用都会新建
    QGraphicsOpacityEffect 并替换旧的；若在淡出动画进行中再次调用
    （连点按钮、Esc + 点击等），正在动画的效果会被中途移除删除，
    出现两个 painter 绘制同一设备：

        QPainter::begin: A paint device can only be painted by one
        painter at a time.
        QPainter::translate: Painter not active

    本 mixin 保证每次显示周期内 done() 只执行一次。
    """

    _fade_closing = False

    def showEvent(self, e):
        self._fade_closing = False
        super().showEvent(e)

    def done(self, code):
        if self._fade_closing:
            return  # 淡出中，忽略重复的关闭请求
        self._fade_closing = True
        super().done(code)


class DarkTitleBar(TitleBar):
    """适配明暗主题的标题栏：窗口图标在最左侧，标题紧随其后。

    颜色动态跟随主题变化（监听 themeChanged），不依赖创建时的主题状态。
    """

    def __init__(self, parent):
        super().__init__(parent)

        # 窗口图标（最左侧，demo 风格：底部对齐）
        self.iconLabel = QLabel(self)
        self.iconLabel.setFixedSize(18, 18)
        self.iconLabel.setStyleSheet("background: transparent;")
        self.hBoxLayout.insertSpacing(0, 10)
        self.hBoxLayout.insertWidget(
            1, self.iconLabel, 0, Qt.AlignLeft | Qt.AlignBottom
        )
        self.window().windowIconChanged.connect(self.setIcon)

        # 窗口标题
        self.titleLabel = QLabel(self)
        self.titleLabel.setObjectName("titleLabel")
        self.hBoxLayout.insertWidget(
            2, self.titleLabel, 0, Qt.AlignLeft | Qt.AlignBottom
        )
        self.window().windowTitleChanged.connect(self.setTitle)

        # 应用当前主题配色，并跟随主题变化动态刷新
        self._apply_theme()
        qconfig.themeChanged.connect(self._apply_theme)

        # 这两个信号可能在标题栏创建前已触发过，主动初始化一次
        self.setTitle(self.window().windowTitle())
        self.setIcon(self.window().windowIcon())

    def _apply_theme(self, *args):
        """根据当前主题设置标题文字与窗口按钮颜色。"""
        dark = isDarkTheme()
        fg = QColor(255, 255, 255) if dark else QColor(0, 0, 0)
        fg_css = "white" if dark else "black"
        hover_bg = QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 26)
        pressed_bg = QColor(255, 255, 255, 51) if dark else QColor(0, 0, 0, 51)

        for btn in (self.minBtn, self.maxBtn, self.closeBtn):
            btn.setNormalColor(fg)
            btn.setHoverColor(fg)
            btn.setPressedColor(fg)
            btn.setNormalBackgroundColor(QColor(0, 0, 0, 0))
            btn.setHoverBackgroundColor(hover_bg)
            btn.setPressedBackgroundColor(pressed_bg)
        # 关闭按钮悬停/按下红色（与 Fluent 风格一致）
        self.closeBtn.setHoverBackgroundColor(QColor(232, 17, 35))
        self.closeBtn.setPressedBackgroundColor(QColor(232, 17, 35, 153))

        self.titleLabel.setStyleSheet(f"color: {fg_css}; background: transparent;")

    def setTitle(self, title):
        self.titleLabel.setText(title)
        self.titleLabel.adjustSize()

    def setIcon(self, icon):
        self.iconLabel.setPixmap(QIcon(icon).pixmap(18, 18))


class _DarkWindowMixin:
    """暗色窗口通用初始化：暗色背景 + 暗色标题栏 + 居中定位。"""

    def _init_dark_window(self):
        self.setTitleBar(DarkTitleBar(self))
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(f"{type(self).__name__}{{background: {DARK_BG};}}")

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_positioned", False):
            self._positioned = True
            self._center_over_parent()

    def _center_over_parent(self):
        """将窗口居中于父窗口；无父窗口时居中于屏幕。"""
        parent = self.parentWidget()
        if parent is not None and parent.window() is not self:
            geo = parent.window().geometry()
        else:
            geo = QApplication.primaryScreen().availableGeometry()

        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + (geo.height() - self.height()) // 2
        self.move(max(geo.x(), x), max(geo.y(), y))


class DarkDialog(_DarkWindowMixin, FramelessDialog):
    """暗色无边框对话框（QDialog 体系）。

    所有弹出窗口的基类：
    - QDialog 原生 exec()/accept()/reject()/done() 模态语义
    - 带 parent 时自动成为独立顶层窗口并居中于父窗口
    - 暗色背景 + DarkTitleBar
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._positioned = False
        self._init_dark_window()


class ConfirmDialog(DarkDialog):
    """确认弹窗。

    用法::

        box = ConfirmDialog("标题", "消息内容", parent)
        box.yesButton.setText("确认")
        if box.exec():
            ...
    """

    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(":/icon.ico"))
        # 对话框风格：仅保留关闭按钮
        self.titleBar.minBtn.hide()
        self.titleBar.maxBtn.hide()

        # 长消息自动增高（每行约 28 个字符）
        lines = max(2, math.ceil(len(message) / 28))
        self.resize(440, min(150 + 20 * lines, 420))

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 48, 20, 20)

        layout.addWidget(StrongBodyLabel(title))

        self.contentLabel = BodyLabel(message)
        self.contentLabel.setWordWrap(True)
        layout.addWidget(self.contentLabel, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancelButton = PushButton("取消")
        self.cancelButton.clicked.connect(self.reject)
        apply_tooltip(self.cancelButton, "取消并关闭窗口")
        btn_row.addWidget(self.cancelButton)

        self.yesButton = PrimaryPushButton("确定")
        self.yesButton.clicked.connect(self.accept)
        apply_tooltip(self.yesButton, "确认操作")
        btn_row.addWidget(self.yesButton)

        layout.addLayout(btn_row)


class InputDialog(MaskFadeGuardMixin, MessageBoxBase):
    """单行输入弹窗（Fluent 风格遮罩弹窗，用于代理地址 / 访问令牌编辑）。

    用法::

        dialog = InputDialog("标题", placeholder="提示", default="默认值", parent=parent)
        if dialog.exec():
            text = dialog.get_text()
    """

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        default: str = "",
        parent=None,
        width: int = 420,
    ):
        super().__init__(parent)
        self.widget.setFixedWidth(width)

        self.titleLabel = StrongBodyLabel(title)
        self.viewLayout.addWidget(self.titleLabel)

        self.lineEdit = LineEdit()
        self.lineEdit.setText(default)
        self.lineEdit.setClearButtonEnabled(True)
        if placeholder:
            self.lineEdit.setPlaceholderText(placeholder)
        self.lineEdit.returnPressed.connect(self.accept)
        self.viewLayout.addWidget(self.lineEdit)

        self.yesButton.setText("确定")
        apply_tooltip(self.yesButton, "确认输入")
        apply_tooltip(self.cancelButton, "取消输入并关闭窗口")

    def get_text(self) -> str:
        return self.lineEdit.text().strip()
