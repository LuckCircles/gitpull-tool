"""qfluentwidgets InfoBarManager 兼容补丁。

针对 qfluentwidgets 1.11.3（.venv/.../qfluentwidgets/components/widgets/info_bar.py）
InfoBarManager.add() 的一处上游缺陷，在**项目内**包裹该方法，
不修改第三方库本身（venv 改动会随环境重装丢失）：

缺陷 — dropAni 无始末值：
    add() 第 356 行创建堆叠下落动画 dropAni 时只 setDuration(200)，
    不设 startValue/endValue；二者只在 remove() → _updateDropAni()
    （第 417 行）中补写。若并行动画组在补写前被启动，Qt 会输出
    "QPropertyAnimation::updateState (pos, InfoBar, ): starting an
    animation without end value" 警告（该位置动画被跳过，提示条仍正常显示）。

补救：add() 成功后立即为 dropAni 补齐始末值。_updateDropAni 之后
会用真实坐标覆盖，不影响正常行为；只是消除"无值窗口"。

（注：slideAni 从未加入并行动画组、remove() 对 dropAni 的摘除也正确，
故无需处理；此处仅包裹 add。）
"""

from __future__ import annotations

_patched = False


def patch_infobar_manager() -> bool:
    """包裹 InfoBarManager.add。返回 True 表示本次执行了补丁（幂等）。"""
    global _patched
    if _patched:
        return False

    from qfluentwidgets.components.widgets.info_bar import InfoBarManager

    orig_add = InfoBarManager.add

    def add(self, infoBar):
        orig_add(self, infoBar)
        # 缺陷补救：dropAni 创建时无始末值，立即补齐，
        # 避免动画组启动时 Qt 报 "without end value" 警告。
        try:
            dropAni = infoBar.property("dropAni")
            if dropAni is not None and dropAni.endValue() is None:
                dropAni.setStartValue(infoBar.pos())
                dropAni.setEndValue(self._pos(infoBar))
        except Exception:
            pass

    InfoBarManager.add = add
    _patched = True
    return True
