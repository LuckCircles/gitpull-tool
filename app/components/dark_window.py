"""通用 Fluent 风格弹窗基类与 UI 助手。

- ConfirmDialog / InputDialog：qfluentwidgets.MessageBoxBase（Fluent 风格遮罩
  弹窗，自动适配明暗主题）。所有弹出窗口统一使用该体系，不再混用
  qframelesswindow 的 FramelessDialog —— 混用时关闭 FramelessDialog 后
  Windows 原生模态状态可能未释放，导致主窗口与后续遮罩弹窗全部无法操作。
- MaskFadeGuardMixin：修复 MaskDialogBase 淡出动画期间重复 done() 的 painter 冲突
- apply_tooltip：Fluent 风格工具提示助手（悬停 300ms 显示）

主窗口使用 qfluentwidgets.MSFluentWindow，不在此文件。
"""

import time

from PySide6.QtCore import QTimer
from qfluentwidgets import (
    BodyLabel,
    InfoBar,
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
    widget.installEventFilter(ToolTipFilter(widget, showDelay=300, position=position))


_FADE_IN_MS = 200  # MaskDialogBase.showEvent 淡入动画时长（毫秒）


class MaskFadeGuardMixin:
    """修复 MaskDialogBase 淡出动画期间的绘制冲突。

    1. 重复 done()：qfluentwidgets 的 MaskDialogBase.done() 每次调用都会
       新建 QGraphicsOpacityEffect 并替换旧的；若在淡出动画进行中再次调用
       （连点按钮、Esc + 点击等），正在动画的效果会被中途移除删除，
       出现两个 painter 绘制同一设备：

           QPainter::begin: A paint device can only be painted by one
           painter at a time.
           QPainter::translate: Painter not active

       本 mixin 保证每次显示周期内 done() 只执行一次。

    2. 淡入期关闭竞态：淡入动画（200ms）结束回调会 setGraphicsEffect(None)。
       若在淡入结束前调用 done()，该回调会移除刚挂上的淡出效果，
       淡出动画随即对已删除的效果逐帧操作 —— 同一 paint device 上产生
       两个 painter（即上述警告）。表现：弹出错误提示后立刻点关闭时
       偶发一次警告。修复：关闭请求落在淡入期内时，延迟到淡入必然
       结束后再执行淡出。

    3. InfoBar 效果嵌套：InfoBar 常驻 QGraphicsOpacityEffect，若弹窗
       淡出时它仍显示（如克隆失败提示 duration 内点关闭），弹窗整体
       的 opacity effect 与其嵌套 —— Qt 不支持嵌套 QGraphicsEffect，
       同样触发上述 painter 冲突。淡出前先隐藏弹窗内所有 InfoBar。
    """

    _fade_closing = False
    _shown_at = 0.0

    def showEvent(self, e):
        self._fade_closing = False
        self._shown_at = time.monotonic()
        super().showEvent(e)

    def done(self, code):
        if self._fade_closing:
            return  # 淡出中，忽略重复的关闭请求
        self._fade_closing = True
        self._dismiss_child_infobars()

        # 关闭落在淡入窗口（200ms）内：延迟到淡入结束后再淡出
        remaining = _FADE_IN_MS - (time.monotonic() - self._shown_at) * 1000
        if self._shown_at > 0 and remaining > 0:
            # context=self：弹窗销毁时定时器自动取消，不会回调已删对象
            QTimer.singleShot(int(remaining) + 20, self, lambda: self._fade_out(code))
        else:
            self._fade_out(code)

    def _fade_out(self, code):
        """执行库的淡出关闭（淡入已结束，效果操作互不交叠）。"""
        super().done(code)

    def _dismiss_child_infobars(self):
        """淡出前隐藏弹窗内的 InfoBar（立即隐藏，不带退出动画）。"""
        for bar in self.findChildren(InfoBar):
            bar.hide()


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
