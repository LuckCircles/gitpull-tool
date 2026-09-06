"""令牌验证工作线程"""

from PySide6.QtCore import QObject, Signal

from core.token_validator import verify_token


class TokenVerifyWorker(QObject):
    """令牌验证工作线程"""

    # 信号
    finished = Signal(bool, str)  # (成功, 消息)

    def __init__(self, token: str | None = None, proxy: str | None = None, timeout: int = 10):
        super().__init__()
        self.token = token
        self.proxy = proxy
        self.timeout = timeout

    def run(self):
        """执行令牌验证"""
        success, message = verify_token(self.token, self.proxy, self.timeout)
        self.finished.emit(success, message)
