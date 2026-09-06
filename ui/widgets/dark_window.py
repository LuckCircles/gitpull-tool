"""通用 Fluent 风格弹窗基类与 UI 助手。

- ConfirmDialog / InputDialog：qfluentwidgets.MessageBoxBase（Fluent 风格遮罩
  弹窗，自动适配明暗主题）。所有弹出窗口统一使用该体系，不再混用
  qframelesswindow 的 FramelessDialog —— 混用时关闭 FramelessDialog 后
  Windows 原生模态状态可能未释放，导致主窗口与后续遮罩弹窗全部无法操作。
- MaskFadeGuardMixin：修复 MaskDialogBase 淡出动画期间重复 done() 的 painter 冲突
- apply_tooltip：Fluent 风格工具提示助手（悬停 300ms 显示）

主窗口使用 qfluentwidgets.MSFluentWindow，不在此文件。
"""

from qfluentwidgets import (
    BodyLabel,
    LineEdit,
    MessageBoxBase,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
)


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


class ConfirmDialog(MaskFadeGuardMixin, MessageBoxBase):
    """确认弹窗（Fluent 风格遮罩弹窗）。

    用法::

        box = ConfirmDialog("标题", "消息内容", parent)
        box.yesButton.setText("确认")
        if box.exec():
            ...
    """

    def __init__(
        self,
        title: str,
        message: str,
        parent=None,
        width: int = 440,
    ):
        super().__init__(parent)
        self.widget.setFixedWidth(width)

        self.viewLayout.addWidget(StrongBodyLabel(title))

        self.contentLabel = BodyLabel(message)
        self.contentLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.contentLabel)

        self.yesButton.setText("确定")
        apply_tooltip(self.yesButton, "确认操作")
        self.cancelButton.setText("取消")
        apply_tooltip(self.cancelButton, "取消并关闭窗口")


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
        self.cancelButton.setText("取消")
        apply_tooltip(self.cancelButton, "取消输入并关闭窗口")

    def get_text(self) -> str:
        return self.lineEdit.text().strip()
