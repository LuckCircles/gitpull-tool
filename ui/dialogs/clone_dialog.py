"""克隆仓库对话框 — Fluent Widgets 风格 UI。

职责：
    - URL 输入、粘贴、格式化
    - 启动 CloneWorker 到 QThread
    - 通过 Signal/Slot 接收 Worker 通知并更新 UI
    - 克隆完成后自动添加仓库到列表并清空输入框
    - 支持连续克隆

线程安全：
    Worker 运行在 QThread 中，所有 UI 操作通过 Signal/Slot 在主线程执行。
    Worker 不直接操作任何 Qt 控件。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QThread
from PySide6.QtGui import QIcon, QTextCursor
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (FluentIcon as FIF)
from qfluentwidgets import (HorizontalSeparator, IndeterminateProgressBar, InfoBar,
                            LineEdit, MessageBox, PrimaryPushButton, PushButton,
                            StrongBodyLabel, SubtitleLabel, TextEdit,
                            ToolButton, ToolTipFilter, ToolTipPosition)

from core.clone_manager import CloneManager
from workers.clone_worker import CloneWorker


class CloneRepoDialog(QDialog):
    """克隆仓库对话框 —— 支持实时输出、连续克隆、线程安全。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._worker: CloneWorker | None = None
        self._thread: QThread | None = None
        self._clone_running = False

        self.setWindowTitle("克隆仓库")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(680, 560)

        # ---- 主布局 ----
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(SubtitleLabel("克隆仓库"))

        # ---- URL 输入行 ----
        url_row = QHBoxLayout()
        url_row.setSpacing(8)

        self.urlLineEdit = LineEdit()
        self.urlLineEdit.setPlaceholderText("输入 Git 仓库链接")
        self.urlLineEdit.setClearButtonEnabled(True)
        self.urlLineEdit.textChanged.connect(self._validate_url)
        url_row.addWidget(self.urlLineEdit, 1)

        self.paste_btn = ToolButton(FIF.PASTE)
        self.paste_btn.installEventFilter(
            ToolTipFilter(self.paste_btn, 0, ToolTipPosition.BOTTOM)
        )
        self.paste_btn.setToolTip("从剪贴板粘贴")
        self.paste_btn.clicked.connect(self._paste_from_clipboard)
        url_row.addWidget(self.paste_btn)

        self.format_btn = ToolButton(FIF.CODE)
        self.format_btn.installEventFilter(
            ToolTipFilter(self.format_btn, 0, ToolTipPosition.BOTTOM)
        )
        self.format_btn.setToolTip("自动格式化链接（例如 git clone 命令 → 纯 URL）")
        self.format_btn.clicked.connect(self._format_url)
        url_row.addWidget(self.format_btn)

        layout.addLayout(url_row)

        # ---- 仓库信息区域 ----
        self._repo_name_label = StrongBodyLabel("仓库名称: -")
        layout.addWidget(self._repo_name_label)

        self._clone_dir_label = StrongBodyLabel("克隆目录: -")
        layout.addWidget(self._clone_dir_label)

        layout.addWidget(HorizontalSeparator())

        # ---- 进度区域 ----
        self._status_label = SubtitleLabel("状态: 就绪")
        layout.addWidget(self._status_label)

        self._progress_bar = IndeterminateProgressBar()
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        # ---- 实时日志窗口 ----
        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        log_header.addWidget(StrongBodyLabel("实时日志"))
        log_header.addStretch()
        self._clear_log_btn = ToolButton(FIF.DELETE)
        self._clear_log_btn.installEventFilter(
            ToolTipFilter(self._clear_log_btn, 0, ToolTipPosition.BOTTOM)
        )
        self._clear_log_btn.setToolTip("清空日志")
        self._clear_log_btn.clicked.connect(self._clear_log)
        log_header.addWidget(self._clear_log_btn)
        layout.addLayout(log_header)

        self._log_edit = TextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setPlaceholderText("克隆开始后，Git 输出将显示在此处...")
        layout.addWidget(self._log_edit, 1)

        # ---- 按钮区域 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._start_btn = PrimaryPushButton(FIF.DOWNLOAD, "克隆")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_clone)
        btn_row.addWidget(self._start_btn)

        self._close_btn = PushButton(FIF.CLOSE, "关闭")
        self._close_btn.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self._close_btn)

        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # 剪贴板粘贴
    # ------------------------------------------------------------------
    def _clear_log(self):
        self._log_edit.clear()

    def _paste_from_clipboard(self):
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text().strip()
            if text:
                self.urlLineEdit.setText(text)

    # ------------------------------------------------------------------
    # 自动格式化（调用 CloneManager.normalize_url）
    # ------------------------------------------------------------------
    def _format_url(self):
        raw = self.urlLineEdit.text().strip()
        if not raw:
            self._paste_from_clipboard()
            raw = self.urlLineEdit.text().strip()
            if not raw:
                return

        url = CloneManager.normalize_url(raw)
        if url and url != raw:
            self.urlLineEdit.setText(url)
        elif url == raw:
            # normalize_github_url 返回 None，已 fallback 到原始输入
            InfoBar.warning("格式化失败", "无法识别为 GitHub 仓库地址，请检查输入", duration=4000, parent=self)

    # ------------------------------------------------------------------
    # URL 验证
    # ------------------------------------------------------------------
    def _validate_url(self):
        if self._clone_running:
            self._start_btn.setEnabled(False)
            return
        url = self.urlLineEdit.text().strip()
        self._start_btn.setEnabled(bool(url))

    def repo_url(self) -> str:
        return self.urlLineEdit.text().strip()

    # ------------------------------------------------------------------
    # 开始克隆（调用 CloneManager.resolve_clone_request 解析请求）
    # ------------------------------------------------------------------
    def _on_start_clone(self):
        raw_url = self.urlLineEdit.text().strip()
        if not raw_url:
            InfoBar.warning("提示", "请输入仓库链接", parent=self)
            return

        # 通过 CloneManager 完整解析克隆请求
        request = CloneManager.resolve_clone_request(raw_url, self._manager.base_dir)
        if request["error"]:
            InfoBar.error("克隆失败", request["error"], duration=5000, parent=self)
            return

        normalized_url = request["normalized_url"]
        repo_name = request["repo_name"]
        target_path = request["target_path"]

        # 清理上一次残留的线程
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None

        # 更新 UI 为克隆中状态
        self._clone_running = True
        self._set_cloning_ui(True)
        self._repo_name_label.setText(f"仓库名称: {repo_name}")
        self._clone_dir_label.setText(f"克隆目录: {target_path}")
        self._log_edit.clear()
        self._start_btn.setEnabled(False)
        self.urlLineEdit.setEnabled(False)
        self.paste_btn.setEnabled(False)
        self.format_btn.setEnabled(False)
        self._close_btn.setText("取消克隆")

        # 创建 Worker 并移入 QThread
        self._worker = CloneWorker(normalized_url, self._manager.base_dir, repo_name)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.output.connect(self._on_output)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self._thread.start()

    # ------------------------------------------------------------------
    # Signal Slots（主线程执行，安全操作 UI）
    # ------------------------------------------------------------------
    def _set_cloning_ui(self, cloning: bool):
        self._progress_bar.setVisible(cloning)
        if cloning:
            self._progress_bar.start()
        else:
            self._progress_bar.stop()

    def _on_progress(self, phase: str):
        self._status_label.setText(f"状态: {phase}")

    def _on_output(self, text: str):
        self._log_edit.append(text)
        cursor = self._log_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._log_edit.setTextCursor(cursor)

    def _on_finished(self, success: bool, message: str):
        self._clone_running = False
        self._set_cloning_ui(False)

        worker = self._worker
        self._worker = None

        # 恢复 UI
        self._start_btn.setEnabled(True)
        self.urlLineEdit.setEnabled(True)
        self.paste_btn.setEnabled(True)
        self.format_btn.setEnabled(True)
        self._close_btn.setText("关闭")

        if success:
            self._status_label.setText("状态: 克隆完成")
            InfoBar.success("克隆成功", message, duration=4000, parent=self)
            # 自动添加到仓库列表
            if worker and os.path.isdir(os.path.join(worker.target_path, ".git")):
                self._manager._add_repo_row(worker.target_path)
                InfoBar.success(
                    "已添加", f"{worker._repo_name} 已添加到仓库列表",
                    duration=4000, parent=self._manager,
                )
            # 清空 URL 准备下一次克隆
            self.urlLineEdit.clear()
            self._repo_name_label.setText("仓库名称: -")
            self._clone_dir_label.setText("克隆目录: -")
            self._status_label.setText("状态: 就绪 — 可继续输入下一个仓库地址")
        else:
            self._status_label.setText("状态: 克隆失败")
            InfoBar.error("克隆失败", message, duration=5000, parent=self)

    # ------------------------------------------------------------------
    # 关闭 / 取消处理
    # ------------------------------------------------------------------
    def _on_close_clicked(self):
        if self._clone_running:
            box = MessageBox(
                "确认关闭", "克隆正在进行中，关闭窗口将中止克隆。\n确定要关闭吗？", self,
            )
            box.yesButton.setText("关闭并中止")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
            self._stop_clone()
        self.close()

    def _stop_clone(self):
        self._clone_running = False
        if self._worker:
            try:
                self._worker.progress.disconnect()
                self._worker.output.disconnect()
                self._worker.finished.disconnect()
            except Exception:
                pass
            self._worker.cancel()
            self._worker = None
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        # 恢复 UI
        self._set_cloning_ui(False)
        self._start_btn.setEnabled(True)
        self.urlLineEdit.setEnabled(True)
        self.paste_btn.setEnabled(True)
        self.format_btn.setEnabled(True)
        self._close_btn.setText("关闭")
        self._status_label.setText("状态: 已取消")

    def closeEvent(self, event):
        if self._clone_running:
            self._stop_clone()
        super().closeEvent(event)
