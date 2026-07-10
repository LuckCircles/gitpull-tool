"""代理验证工作线程"""

from PySide6.QtCore import QObject, Signal

from core.proxy_validator import verify_proxy


class ProxyVerifyWorker(QObject):
    """代理验证工作线程"""

    # 信号
    finished = Signal(bool, str)  # (成功, 消息)

    def __init__(self, proxy: str | None = None, timeout: int = 10):
        super().__init__()
        self.proxy = proxy
        self.timeout = timeout

    def run(self):
        """执行代理验证"""
        success, message = verify_proxy(self.proxy, self.timeout)
        self.finished.emit(success, message)
