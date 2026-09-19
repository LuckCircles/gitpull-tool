"""Qt 任务执行器 — 参考 pyqt5-concurrent 的面向任务并发设计。

基于 QThreadPool + QRunnable 实现（QThread + moveToThread 的轻量替代）：
- TaskExecutor.run(target, *args, **kwargs) 提交任意函数，返回 QFuture
- QFuture 提供 result / failed / finished 信号与 then(on_success, on_failed) 链式回调
- 任务异常统一捕获进 failed 信号（后台线程异常不会崩 UI，并写入日志）
- 线程池全局复用，无逐次创建/销毁 QThread 的开销，也无退出时的析构等待样板

适用：一次性轻量任务（网络请求、git 命令序列、数据加载）。
不适用：流式进度回传的长任务（如克隆）和子进程管理（如资产下载），仍用专用 Worker/QProcess。
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

from loguru import logger
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class QFuture(QObject):
    """任务的未来结果：result(成功返回值) / failed(异常信息) / finished。"""

    result = Signal(object)
    failed = Signal(str)
    finished = Signal()
    # 内部信号：任务结束后释放保活引用（不受 detach 影响）。
    # 必须经信号而非 QTimer.singleShot —— 后者从工作线程调用时定时器
    # 归属无事件循环的池线程，永远不会触发。
    _release = Signal()

    def __init__(self):
        super().__init__()
        self._done = False
        # 自连接：工作线程 emit 后经队列连接投递到主线程（future 的创建线程）
        self._release.connect(self._self_release)

    def isFinished(self) -> bool:
        return self._done

    def then(
        self,
        on_success: Callable | None = None,
        on_failed: Callable | None = None,
    ) -> "QFuture":
        """链式注册回调（主线程执行），返回自身可继续链式。"""
        if on_success is not None:
            self.result.connect(on_success)
        if on_failed is not None:
            self.failed.connect(on_failed)
        return self

    def detach(self):
        """断开全部信号（窗口关闭时丢弃后续回调，避免触达已销毁控件）。

        future 对象仍由 TaskExecutor._futures 保活直至任务结束，
        因此 detach 后任务正常跑完，只是结果不再回传。
        """
        # PySide6 对断开未连接的信号仅发出 RuntimeWarning（不抛异常）
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for sig in (self.result, self.failed, self.finished):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass

    def _self_release(self):
        """在主线程释放保活引用 —— 此时 queued 的 result/failed/finished 事件均已投递完毕。"""
        TaskExecutor._futures.discard(self)


class _Task(QRunnable):
    """QRunnable 模板：统一执行目标函数并捕获异常（任务与 run 解耦）。"""

    def __init__(self, future: QFuture, target: Callable, args: tuple, kwargs: dict):
        super().__init__()
        self._future = future
        self._target = target
        self._args = args
        self._kwargs = kwargs

    def run(self):  # noqa: D102 — QRunnable 线程池入口
        try:
            value = self._target(*self._args, **self._kwargs)
        except Exception as e:  # noqa: BLE001 后台线程兜底，异常经信号回传
            logger.opt(exception=True).error(f"[Task] 任务执行异常: {self._target}")
            self._future.failed.emit(str(e))
        else:
            self._future.result.emit(value)
        finally:
            self._future._done = True
            self._future.finished.emit()
            self._future._release.emit()


class TaskExecutor:
    """全局任务执行器（全局线程池，跨窗口复用）。"""

    _pool = QThreadPool.globalInstance()
    _futures: set[QFuture] = set()  # 运行中任务的保活引用

    @classmethod
    def run(cls, target: Callable, *args: Any, **kwargs: Any) -> QFuture:
        """提交任务到线程池并返回 future。

        回调经信号在主线程执行（跨线程自动队列连接），可安全操作 UI。
        """
        future = QFuture()
        cls._futures.add(future)  # 保活：信号投递完成前不被 GC
        cls._pool.start(_Task(future, target, args, kwargs))
        return future

    @classmethod
    def setMaxThreads(cls, count: int):
        """设置线程池最大线程数（应用启动时调用一次）。

        全局池默认 maxThreadCount = CPU 核心数，批量 git/fetch 等
        IO 任务并发较高时可在入口处调高。
        """
        cls._pool.setMaxThreadCount(max(1, count))

    @classmethod
    def waitForDone(cls, timeout_ms: int = 5000) -> bool:
        """等待所有任务完成（应用退出前调用）。"""
        return cls._pool.waitForDone(timeout_ms)
