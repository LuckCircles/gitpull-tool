"""应用入口 — 薄入口：只做装配（日志/主题/补丁/窗口），业务在 app/view/。

分层结构（借鉴 Fluent-M3U8）：
    app/common/     基础设施：配置/路径/日志/令牌/网络校验/工具函数/并发执行器
    app/service/    业务服务：git 执行与操作/克隆/更新/Release/探针 + Qt Worker
    app/components/ 复用 UI 组件：对话框/版本历史页/弹窗基类
    app/view/       界面：主窗口 GitManager
    app/resource/   资源：图标/qrc/内嵌工具（deno + download.ts）
"""
import contextlib
import io
import sys

# 提前加载 qfluentwidgets 配置模块，屏蔽其推广信息（print 到 stdout）
with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets.common.config  # noqa: F401

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from app.common.infobar_compat import patch_infobar_manager
from app.common.logging_setup import init_logging, qt_message_handler
from app.view.main_window import GitManager


def main():
    app = QApplication(sys.argv)
    # 日志初始化 + 旧版数据迁移（须在 Qt 消息处理器与任何窗口之前）
    init_logging()
    # 捕获 Qt 内部警告/错误到日志文件（需在创建任何 Qt 对象前安装）
    qInstallMessageHandler(qt_message_handler)
    # 修补 qfluentwidgets InfoBarManager 的上游缺陷（需在首个 InfoBar 显示前调用）
    patch_infobar_manager()
    # 在创建任何窗口/控件前设置暗色主题，避免控件先按亮色初始化再切换
    setTheme(Theme.DARK)
    window = GitManager()
    window.center_window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
