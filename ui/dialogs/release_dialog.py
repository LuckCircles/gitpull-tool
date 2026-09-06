"""Release 查看对话框 — Fluent 遮罩弹窗。

上半部分为 Release 版本列表（点击切换），下半部分为选中版本的资产表格，
选中资产后点击「下载资产」经 deno 下载到仓库所在目录。
"""

import os

from loguru import logger
from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QFileDialog, QHeaderView, QTableWidget, QTableWidgetItem
from qfluentwidgets import (
    IndeterminateProgressBar,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)

from ui.widgets.dark_window import MaskFadeGuardMixin, apply_tooltip
from workers.release_worker import AssetDownloadManager, ReleaseListWorker


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


class ReleaseDialog(MaskFadeGuardMixin, MessageBoxBase):
    """Release 查看与资产下载对话框"""

    # 类级引用：对话框销毁后仍在执行的加载线程不被 GC
    _active_workers: set = set()

    def __init__(
        self,
        repo_path: str,
        remote_url: str,
        token: str | None,
        proxy: str | None,
        parent=None,
    ):
        super().__init__(parent)
        self.repo_path = repo_path
        self._token = token
        self._proxy = proxy
        repo_name = os.path.basename(repo_path.rstrip("\\/"))

        self._thread: QThread | None = None
        self._worker: ReleaseListWorker | None = None
        self._releases: list[dict] = []
        self._current_release: dict | None = None

        self.widget.setFixedWidth(880)

        # 遮罩弹窗无标题栏，标题显示在内容区顶部
        self.viewLayout.addWidget(StrongBodyLabel(f"Release · {repo_name}"))

        self.progress = IndeterminateProgressBar()
        self.progress.setVisible(False)
        self.viewLayout.addWidget(self.progress)

        # ---------- Release 列表 ----------
        self.release_table = TableWidget()
        self.release_table.setColumnCount(4)
        self.release_table.setHorizontalHeaderLabels(
            ["版本", "名称", "发布日期", "类型"]
        )
        self.release_table.verticalHeader().setVisible(False)
        self.release_table.setFixedHeight(180)
        # 列宽分配：版本/日期自适应内容，名称固定较窄宽度（超长省略+悬停看全名），
        # 类型列弹性填充剩余空间（值居中），整表无右侧留白
        self.release_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.release_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.release_table.setColumnWidth(1, 260)
        self.release_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.release_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.release_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.release_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.release_table.itemSelectionChanged.connect(self._on_release_selected)
        self.viewLayout.addWidget(self.release_table)

        # ---------- 资产表格 ----------
        self.viewLayout.addWidget(StrongBodyLabel("资产文件"))
        self.asset_table = TableWidget()
        self.asset_table.setColumnCount(4)
        self.asset_table.setHorizontalHeaderLabels(["文件名", "大小", "日期", "下载次数"])
        self.asset_table.verticalHeader().setVisible(False)
        self.asset_table.setFixedHeight(200)
        # 列宽分配：文件名列固定上限（收窄，超长省略+悬停看全名），其余列自适应内容，
        # 下载次数列弹性填充剩余空间
        self.asset_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.asset_table.setColumnWidth(0, 300)
        self.asset_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.asset_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.asset_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.asset_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.viewLayout.addWidget(self.asset_table)

        # ---------- 下载进度条 ----------
        self.download_progress = ProgressBar()
        self.download_progress.setVisible(False)
        self.viewLayout.addWidget(self.download_progress)

        # ---------- 下载管理器 ----------
        self.download_manager = AssetDownloadManager(self)
        self.download_manager.progress.connect(self._on_download_progress)
        self.download_manager.finished.connect(self._on_download_finished)

        # ---------- 底部按钮 ----------
        self.yesButton.hide()
        self.download_btn = PushButton("下载资产")
        apply_tooltip(self.download_btn, "下载选中的资产文件到本地")
        self.download_btn.clicked.connect(self.download_asset)
        self.download_btn.setEnabled(False)
        self.buttonLayout.insertWidget(0, self.download_btn, 1, Qt.AlignVCenter)

        self.cancelButton.setText("关闭窗口")
        apply_tooltip(self.cancelButton, "关闭 Release 窗口")

        self._load(remote_url, token, proxy)

    # ------------------------------------------------------------------
    # 列表加载
    # ------------------------------------------------------------------
    def _load(self, remote_url: str, token: str | None, proxy: str | None):
        self._set_loading(True)

        self._worker = ReleaseListWorker(remote_url, token, proxy)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_loaded)

        key = (self._thread, self._worker)
        ReleaseDialog._active_workers.add(key)
        self._thread.finished.connect(
            lambda: ReleaseDialog._active_workers.discard(key)
        )
        self._thread.start()

    def _set_loading(self, loading: bool):
        if loading:
            self.progress.setVisible(True)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.setVisible(False)

    def _on_loaded(self, releases: list, error: str):
        self._set_loading(False)
        if error:
            logger.error(f"[Release] 加载失败: {error}")
            self.release_table.setRowCount(1)
            self.release_table.setItem(0, 0, QTableWidgetItem(f"加载失败: {error}"))
            return
        self._releases = releases
        self._fill_releases()
        # 默认选中第一个（最新）版本
        if releases:
            self.release_table.selectRow(0)

    def _fill_releases(self):
        self.release_table.setRowCount(0)
        for rel in self._releases:
            row = self.release_table.rowCount()
            self.release_table.insertRow(row)
            self.release_table.setItem(row, 0, QTableWidgetItem(rel["tag"]))
            # 名称列 Stretch 填充剩余宽度，超长自动省略，悬停可看全名
            name_item = QTableWidgetItem(rel["name"])
            name_item.setToolTip(rel["name"])
            self.release_table.setItem(row, 1, name_item)
            self.release_table.setItem(row, 2, QTableWidgetItem(rel["published_at"]))
            self.release_table.setItem(
                row, 3, QTableWidgetItem("预发布" if rel["prerelease"] else "正式版")
            )
            # 行数据挂在第 0 列
            self.release_table.item(row, 0).setData(Qt.UserRole, rel)

    def _on_release_selected(self):
        items = self.release_table.selectedItems()
        if not items:
            return
        rel = self.release_table.item(items[0].row(), 0).data(Qt.UserRole)
        if not rel or rel is self._current_release:
            return
        self._current_release = rel
        self._fill_assets(rel)

    def _fill_assets(self, rel: dict):
        self.asset_table.setRowCount(0)
        for asset in rel["assets"]:
            row = self.asset_table.rowCount()
            self.asset_table.insertRow(row)
            name_item = QTableWidgetItem(asset["name"])
            name_item.setToolTip(asset["name"])  # 超长文件名悬停查看全名
            self.asset_table.setItem(row, 0, name_item)
            self.asset_table.setItem(row, 1, QTableWidgetItem(_fmt_size(asset["size"])))
            self.asset_table.setItem(row, 2, QTableWidgetItem(asset.get("updated_at", "")))
            self.asset_table.setItem(
                row, 3, QTableWidgetItem(str(asset["download_count"]))
            )
            self.asset_table.item(row, 0).setData(Qt.UserRole, asset)
        self.download_btn.setEnabled(bool(rel["assets"]))

    # ------------------------------------------------------------------
    # 资产下载
    # ------------------------------------------------------------------
    def download_asset(self):
        if self.download_manager.is_running:
            return
        items = self.asset_table.selectedItems()
        if not items:
            return
        asset = self.asset_table.item(items[0].row(), 0).data(Qt.UserRole)
        if not asset:
            return

        # 默认保存到仓库同级目录，用户可改
        default_path = os.path.join(
            os.path.dirname(self.repo_path.rstrip("\\/")) or ".",
            asset["name"],
        )
        save_path, _ = QFileDialog.getSaveFileName(
            self, "保存资产文件", default_path
        )
        if not save_path:
            return

        self.download_btn.setEnabled(False)
        self.download_progress.setVisible(True)
        self.download_progress.setValue(0)
        # 由调用方注入 token/proxy（构造时保存）
        self.download_manager.start(
            asset["url"],
            asset["name"],
            save_path,
            self._token,
            self._proxy,
        )

    def _on_download_progress(self, received: int, total: int, name: str):
        if total > 0:
            self.download_progress.setMaximum(total)
            self.download_progress.setValue(min(received, total))
        else:
            # 未知大小：显示不确定进度
            self.download_progress.setMaximum(0)

    def _on_download_finished(self, success: bool, message: str):
        self.download_progress.setVisible(False)
        if self._current_release:
            self.download_btn.setEnabled(bool(self._current_release["assets"]))
        if success:
            from qfluentwidgets import InfoBar

            InfoBar.success("下载完成", message, parent=self)
        else:
            from qfluentwidgets import InfoBar

            InfoBar.error("下载失败", message[:150], parent=self)

    # ------------------------------------------------------------------
    # 关闭清理
    # ------------------------------------------------------------------
    def done(self, code):
        # 关窗时若仍在下载则取消（deno 脚本会清理半成品文件）
        self.download_manager.cancel()
        self._stop_loading_thread()
        super().done(code)

    @classmethod
    def shutdown_active_workers(cls, timeout_ms: int = 3000):
        """应用退出前停止并等待所有仍在运行的加载线程。"""
        for thread, worker in list(cls._active_workers):
            try:
                worker.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            thread.quit()
            thread.wait(timeout_ms)

    def _stop_loading_thread(self):
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        if thread:
            if worker:
                try:
                    worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
            thread.quit()
