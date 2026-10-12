"""Release 查看对话框 — 仿 GitHub 网页端布局。

左侧为版本列表（tag + 日期），右侧上部为选中版本的发布信息
（标题 / 徽章 / 发布说明，markdown 轻量渲染），右侧下部为资产
文件表格（附加 Source code zip/tar.gz 两条虚拟资产）。
点击「下载资产」：已配置下载目录时直接下载（重名自动加序号），
否则弹窗确认保存位置（默认仓库所在目录）。
"""

import os
import re
import time
from urllib.parse import quote

import markdown
from loguru import logger
from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    HorizontalSeparator,
    IndeterminateProgressBar,
    ListWidget,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TextBrowser,
    ToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from core.release_service import extract_github_owner_repo
from ui.widgets.dark_window import MaskFadeGuardMixin, apply_tooltip
from utils.qt_executor import QFuture, TaskExecutor
from workers.release_worker import AssetDownloadManager, fetch_release_list


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _fmt_speed(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_count(n) -> str:
    """下载次数紧凑格式（GitHub stars 风格）：1000→1k、1234→1.2k、12k、1M、1.2M。

    便于在窄列里完整显示，避免原始大数字被截断；完整值由单元格 tooltip 提供。
    """
    if n is None:
        return "-"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n < 0:
        return str(n)
    if n < 1000:
        return str(n)

    def _compact(div: int, suffix: str) -> str:
        v = n / div
        if v < 10:
            s = f"{v:.1f}".rstrip("0").rstrip(".")  # 1.2 / 1（x.0 去尾零）
        else:
            s = str(int(v))  # 12、123
        return s + suffix

    if n < 1_000_000:
        return _compact(1000, "k")
    if n < 1_000_000_000:
        return _compact(1_000_000, "M")
    return _compact(1_000_000_000, "B")


# ---------------------------------------------------------------------------
# 发布说明 markdown → HTML（python-markdown 库，GitHub 风格）
# ---------------------------------------------------------------------------
# fenced_code: ``` 围栏代码块；tables: GFM 表格；
# nl2br: 单换行即 <br>（GitHub 行为）；sane_lists: 列表语法修正
_MD_EXTENSIONS = ("fenced_code", "tables", "nl2br", "sane_lists")


def _md_to_html(md: str) -> str:
    """Release body → HTML（markdown 库渲染 + Qt 富文本适配后处理）。"""
    if not md or not md.strip():
        return "<p>（无发布说明）</p>"
    html = markdown.markdown(md, extensions=list(_MD_EXTENSIONS))
    # TextBrowser 不加载远程图片（避免裂图/隐私请求），占位套 code 样式成小标签
    html = re.sub(r"<img[^>]*>", "<code>[图片]</code>", html)
    # 代码块：Qt 对块级 background 支持有限，用单元格背景包装出深色容器
    html = re.sub(
        r"<pre>(.*?)</pre>",
        r'<table width="100%" cellspacing="0" cellpadding="10"'
        r' style="background-color: rgba(0,0,0,80);'
        r' border: 1px solid rgba(255,255,255,35);">'
        r"<tr><td><pre>\1</pre></td></tr></table>",
        html,
        flags=re.DOTALL,
    )
    # 表格：开启网格线（Qt 在单元格间隙中绘制边框，cellspacing 需 ≥1 才有画线空间）
    html = html.replace(
        "<table>",
        '<table cellspacing="1" cellpadding="4" border="1" '
        'style="border-color: rgba(255,255,255,150);">',
    )
    # 引用块：Qt 不支持 border-left，用左侧色条单元格模拟竖线（nbsp 防空单元格塌陷）
    html = re.sub(
        r"<blockquote>(.*?)</blockquote>",
        r'<table cellspacing="0" cellpadding="0"><tr>'
        r'<td width="4" style="background-color: rgba(255,255,255,170);">&nbsp;</td>'
        r'<td width="12"></td>'
        r'<td style="color: rgba(255,255,255,170);">\1</td></tr></table>',
        html,
        flags=re.DOTALL,
    )
    return html


class ReleaseDialog(MaskFadeGuardMixin, MessageBoxBase):
    """Release 查看与资产下载对话框（仿 GitHub 网页端左右布局）"""

    def __init__(
        self,
        repo_path: str,
        remote_url: str,
        token: str | None,
        proxy: str | None,
        parent=None,
        download_dir: str | None = None,
    ):
        super().__init__(parent)
        self.repo_path = repo_path
        self._token = token
        self._proxy = proxy
        # 下载目录：设置页配置的 Release 下载目录；未配置时保存到仓库所在目录
        self._download_dir = (download_dir or "").strip() or None
        # owner/repo：构造 Source code 的 archive 下载链接
        self._owner_repo = extract_github_owner_repo(remote_url)
        repo_name = os.path.basename(repo_path.rstrip("\\/"))

        self._load_future: QFuture | None = None
        self._releases: list[dict] = []
        self._current_release: dict | None = None

        self.widget.setFixedWidth(1000)

        # 遮罩弹窗无标题栏，标题显示在内容区顶部
        self.viewLayout.addWidget(StrongBodyLabel(f"Release · {repo_name}"))

        # ---------- 主体：左侧版本列表 + 右侧信息/资产（加载完成后显示） ----------
        self._body_container = QWidget()
        body = QHBoxLayout(self._body_container)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(16)

        # 左侧：版本列表卡片（仿 GitHub tag 侧栏）
        # CardWidget 默认 Preferred 尺寸策略会与右列抢宽度，需固定：
        # 卡片宽 = 列表 200 + 内边距 16
        left_card = CardWidget()
        left_card.setFixedWidth(216)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(8, 8, 8, 8)
        self.tag_list = ListWidget()
        self.tag_list.setFixedWidth(200)
        self.tag_list.setMinimumHeight(470)
        self.tag_list.itemSelectionChanged.connect(self._on_release_selected)
        left_layout.addWidget(self.tag_list)
        body.addWidget(left_card)

        # 右侧：发布信息 + 资产文件共用一张卡片
        right_card = CardWidget()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(8)

        self.release_title = SubtitleLabel("")
        right_layout.addWidget(self.release_title)

        self.release_meta = BodyLabel("")
        right_layout.addWidget(self.release_meta)

        right_layout.addWidget(HorizontalSeparator())

        self.body_view = TextBrowser()
        self.body_view.setOpenExternalLinks(True)
        self.body_view.setFixedHeight(180)
        self.body_view.document().setDefaultStyleSheet(
            "a { color: #4cc2ff; }"
            "h1,h2,h3,h4,h5,h6 { color: rgba(255,255,255,228); }"
            "code { background: rgba(255,255,255,26);"
            " font-family: Consolas, 'Courier New', monospace; }"
            "pre { font-family: Consolas, 'Courier New', monospace; }"
            "pre code { background: transparent; }"
            "table { border-color: rgba(255,255,255,150); }"
            "th { background: rgba(255,255,255,32); font-weight: bold;"
            " padding: 3px 10px; }"
            "td { padding: 3px 10px; }"
            "hr { border: none; border-top: 1px solid rgba(255,255,255,50); }"
        )
        right_layout.addWidget(self.body_view)

        self.asset_table = TableWidget()
        self.asset_table.setColumnCount(4)
        # 水平表头：qfluentwidgets TableWidget 内部有代理表头，
        # setVisible(False) 对其不生效——清空标签 + 强制隐藏
        self.asset_table.setHorizontalHeaderLabels(["", "", "", ""])
        self.asset_table.horizontalHeader().hide()
        self.asset_table.verticalHeader().hide()
        self.asset_table.setFixedHeight(200)
        # 列宽分配：文件名列弹性占大头，大小列自适应内容，
        # 日期/次数列固定窄宽（次数仅容纳 4-5 位数字）
        self.asset_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.asset_table.setColumnWidth(0, 380)
        self.asset_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.asset_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Interactive
        )
        self.asset_table.setColumnWidth(2, 110)
        self.asset_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Interactive
        )
        self.asset_table.setColumnWidth(3, 70)
        self.asset_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        right_layout.addWidget(self.asset_table)

        body.addWidget(right_card)

        self._body_container.setVisible(False)
        self.viewLayout.addWidget(self._body_container)

        # ---------- 下部进度条区域（常驻：加载/下载共用，控件按需显示） ----------
        self.progress_area = QFrame()
        self.progress_area.setObjectName("releaseProgressArea")
        self.progress_area.setStyleSheet(
            "QFrame#releaseProgressArea {"
            " background: rgba(255,255,255,10); border-radius: 6px; }"
        )
        progress_layout = QVBoxLayout(self.progress_area)
        progress_layout.setContentsMargins(12, 6, 12, 6)
        progress_layout.setSpacing(4)

        # 列表加载：不确定进度条
        self.load_progress = IndeterminateProgressBar()
        self.load_progress.setVisible(False)
        # 资产下载行：确定进度条 + 不确定进度条 + 打开文件夹按钮（下载成功后出现）
        # 说明：qfluentwidgets 的 ProgressBar 在 setMaximum(0) 时不渲染忙碌动画
        #（paintEvent 命中 `minimum()>=maximum()` 早退，只画背景细线），
        # 因此未知大小下载必须改用 IndeterminateProgressBar 提供动态反馈，
        # 已知大小仍用确定 ProgressBar。两者同处一行，按 total 互斥显示。
        self.download_progress = ProgressBar()
        self.download_progress.setVisible(False)
        self.download_progress_busy = IndeterminateProgressBar(start=False)
        self.download_progress_busy.setVisible(False)
        self._last_download_dir = ""
        self.open_folder_btn = PushButton(FIF.FOLDER, "打开文件夹")
        self.open_folder_btn.setVisible(False)
        self.open_folder_btn.clicked.connect(self._open_download_folder)
        apply_tooltip(self.open_folder_btn, "打开本次下载所在文件夹")
        # 下载控制：暂停/恢复 + 取消（ToolButton，下载中/已暂停时可见）
        self.pause_btn = ToolButton(FIF.PAUSE)
        apply_tooltip(self.pause_btn, "暂停 / 恢复下载")
        self.pause_btn.setVisible(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.cancel_btn = ToolButton(FIF.CANCEL)
        apply_tooltip(self.cancel_btn, "取消下载（删除半成品）")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_download)
        download_row = QHBoxLayout()
        download_row.setContentsMargins(0, 0, 0, 0)
        download_row.addWidget(self.download_progress, 1)
        download_row.addWidget(self.download_progress_busy, 1)
        download_row.addWidget(self.pause_btn)
        download_row.addWidget(self.cancel_btn)
        download_row.addWidget(self.open_folder_btn)
        self.download_status = BodyLabel("")
        self.download_status.setFixedHeight(20)
        self.download_status.setVisible(False)
        progress_layout.addWidget(self.load_progress)
        progress_layout.addLayout(download_row)
        progress_layout.addWidget(self.download_status)
        # 最小高度：空态（无加载/下载）区域仍保持可见；有内容时自动撑高
        self.progress_area.setMinimumHeight(64)
        self.viewLayout.addWidget(self.progress_area)

        # ---------- 下载管理器 ----------
        self.download_manager = AssetDownloadManager(self)
        self.download_manager.progress.connect(self._on_download_progress)
        self.download_manager.finished.connect(self._on_download_finished)
        self.download_manager.paused.connect(self._on_download_paused)

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

        self._load_future = (
            TaskExecutor.run_ui(fetch_release_list, remote_url, token, proxy)
            .then(
                on_success=lambda result: self._on_loaded(*result),
                on_failed=lambda message: self._on_loaded([], message),
            )
        )

    def _set_loading(self, loading: bool):
        if loading:
            self.load_progress.setVisible(True)
            self.load_progress.start()
            self.download_status.setText("正在加载 Release 列表 …")
            self.download_status.setVisible(True)
        else:
            self.load_progress.stop()
            self.load_progress.setVisible(False)

    def _on_loaded(self, releases: list, error: str):
        self._set_loading(False)
        self._body_container.setVisible(True)
        if error:
            logger.error(f"[Release] 加载失败: {error}")
            self.tag_list.clear()
            item = QListWidgetItem(f"加载失败\n{error[:60]}")
            item.setSizeHint(QSize(180, 46))
            self.tag_list.addItem(item)
            self.release_title.setText("加载失败")
            self.release_meta.setText(error)
            self.body_view.setHtml("<p>（无法加载 Release 列表）</p>")
            self.download_status.setText(f"加载失败：{error[:80]}")
            return
        self.download_status.setVisible(False)
        self._releases = releases
        self._fill_releases()
        # 默认选中第一个（最新）版本
        if releases:
            self.tag_list.setCurrentRow(0)

    def _latest_tag(self) -> str | None:
        """最新正式版 tag（列表已按日期倒序，首个非预发布即 Latest）。"""
        for rel in self._releases:
            if not rel["prerelease"]:
                return rel["tag"]
        return None

    def _fill_releases(self):
        self.tag_list.clear()
        latest = self._latest_tag()
        for rel in self._releases:
            tag = rel["tag"]
            badges = []
            if tag == latest:
                badges.append("Latest")
            if rel["prerelease"]:
                badges.append("预发布")
            # 列表项不显示发布时间（日期保留在悬停提示中）
            sub = " · ".join(badges)
            date = rel["published_at"] or "未发布"
            item = QListWidgetItem(f"{tag}\n{sub}" if sub else tag)
            item.setSizeHint(QSize(180, 46 if sub else 32))
            item.setToolTip(f"{rel['name']}\n{tag} · {date}")
            item.setData(Qt.UserRole, rel)
            self.tag_list.addItem(item)

    def _on_release_selected(self):
        item = self.tag_list.currentItem()
        if not item:
            return
        rel = item.data(Qt.UserRole)
        if not rel or rel is self._current_release:
            return
        self._current_release = rel
        self._fill_info(rel)
        self._fill_assets(rel)

    # ------------------------------------------------------------------
    # 右侧发布信息
    # ------------------------------------------------------------------
    def _fill_info(self, rel: dict):
        self.release_title.setText(rel["name"])
        latest = self._latest_tag() == rel["tag"]
        if latest:
            kind = "Latest"
        elif rel["prerelease"]:
            kind = "预发布"
        else:
            kind = "正式版"
        parts = [kind, rel["tag"]]
        if rel["published_at"]:
            parts.append(f"发布于 {rel['published_at']}")
        self.release_meta.setText(" · ".join(parts))
        self.body_view.setHtml(_md_to_html(rel.get("body", "")))
        self.body_view.moveCursor(QTextCursor.Start)

    # ------------------------------------------------------------------
    # 资产表格（含 Source code）
    # ------------------------------------------------------------------
    def _source_code_assets(self, rel: dict) -> list[dict]:
        """构造 Source code 虚拟资产（GitHub 网页端同款 zip/tar.gz）。"""
        if not self._owner_repo:
            return []
        owner, repo = self._owner_repo
        tag = quote(rel["tag"], safe="")
        date = rel.get("published_at") or ""
        return [
            {
                "name": f"Source code ({ext})",
                "save_name": f"{repo}-{rel['tag']}.{ext}",
                "size": None,  # archive 无大小元数据
                "download_count": None,
                "url": f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.{ext}",
                "updated_at": date,
            }
            for ext in ("zip", "tar.gz")
        ]

    def _fill_assets(self, rel: dict):
        self.asset_table.setRowCount(0)
        assets = list(rel["assets"]) + self._source_code_assets(rel)
        for asset in assets:
            row = self.asset_table.rowCount()
            self.asset_table.insertRow(row)
            name_item = QTableWidgetItem(asset["name"])
            name_item.setToolTip(asset["name"])  # 超长文件名悬停查看全名
            self.asset_table.setItem(row, 0, name_item)
            size = asset["size"]
            self.asset_table.setItem(
                row, 1, QTableWidgetItem(_fmt_size(size) if size is not None else "-")
            )
            self.asset_table.setItem(
                row, 2, QTableWidgetItem(asset.get("updated_at", "") or "-")
            )
            count = asset.get("download_count")
            count_item = QTableWidgetItem(_fmt_count(count))
            # 紧凑显示（1.2k / 12k / 1.2M）防窄列截断；tooltip 给完整次数便于查看
            count_item.setToolTip(str(count) if count is not None else "-")
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.asset_table.setItem(row, 3, count_item)
            self.asset_table.item(row, 0).setData(Qt.UserRole, asset)
        self.download_btn.setEnabled(bool(assets))

    # ------------------------------------------------------------------
    # 资产下载
    # ------------------------------------------------------------------
    @staticmethod
    def _unique_save_path(base: str, name: str) -> str:
        """直接返回目标路径——同文件名覆盖，不生成序号副本。"""
        return os.path.join(base, name)

    def download_asset(self):
        if self.download_manager.is_running:
            return
        items = self.asset_table.selectedItems()
        if not items:
            return
        asset = self.asset_table.item(items[0].row(), 0).data(Qt.UserRole)
        if not asset:
            return
        # Source code 保存名用 repo-tag 扩展名（与 GitHub 网页端下载一致）
        file_name = asset.get("save_name") or asset["name"]

        if self._download_dir and os.path.isdir(self._download_dir):
            # 已配置有效下载目录 → 直接下载，重名自动加序号，不弹窗
            save_path = self._unique_save_path(self._download_dir, file_name)
        else:
            # 未配置或目录已被删除 → 弹窗确认（默认仓库所在目录）
            base = os.path.dirname(self.repo_path.rstrip("\\/")) or "."
            default_path = os.path.join(base, file_name)
            save_path, _ = QFileDialog.getSaveFileName(self, "保存资产文件", default_path)
            if not save_path:
                return

        self.download_btn.setEnabled(False)
        self.open_folder_btn.setVisible(False)
        # 显示暂停 / 取消控制按钮；暂停按钮初始为 PAUSE 图标
        self.pause_btn.setIcon(FIF.PAUSE)
        self.pause_btn.setVisible(True)
        self.cancel_btn.setVisible(True)
        # 下载开始时大小未知 → 先用不确定进度条给即时动态反馈，
        # 首个 total>0 的进度信号到达后切换为确定进度条。
        self.download_progress.setValue(0)
        self.download_progress.setVisible(False)
        self.download_progress_busy.setVisible(True)
        if not self.download_progress_busy.isStarted():
            self.download_progress_busy.start()
        # 记录本次下载目录（成功后供"打开文件夹"使用）
        self._last_download_dir = os.path.dirname(save_path)
        # 进度采样窗口重置（速度 = 窗口内字节增量 / 时间增量）
        self._speed_samples: list[tuple[float, int]] = [(time.monotonic(), 0)]
        self.download_status.setText(f"正在下载 {file_name} …")
        self.download_status.setVisible(True)
        # 由调用方注入 token/proxy（构造时保存）
        self.download_manager.start(
            asset["url"],
            file_name,
            save_path,
            self._token,
            self._proxy,
        )

    def _on_download_progress(self, received: int, total: int, name: str):
        if total > 0:
            # 已知大小：切换为确定进度条
            if self.download_progress_busy.isVisible():
                self.download_progress_busy.stop()
                self.download_progress_busy.setVisible(False)
                self.download_progress.setVisible(True)
            self.download_progress.setMaximum(total)
            self.download_progress.setValue(min(received, total))
        else:
            # 未知大小：不确定进度条做忙碌指示
            #（qfluentwidgets ProgressBar 的 setMaximum(0) 不渲染动画，已弃用）
            if not self.download_progress.isVisible():
                self.download_progress_busy.setVisible(True)
                if not self.download_progress_busy.isStarted():
                    self.download_progress_busy.start()

        # 速度：最近 3 秒采样窗口内的平均速率（瞬时值抖动大，不做）
        now = time.monotonic()
        samples = getattr(self, "_speed_samples", None) or [(now, received)]
        samples.append((now, received))
        # 移除窗口外旧采样，但至少保留最早一条用于计算
        while len(samples) > 2 and now - samples[0][0] > 3.0:
            samples.pop(0)
        dt = now - samples[0][0]
        speed = (received - samples[0][1]) / dt if dt > 0.15 else 0.0

        # 文本：已下载 / 总大小 · 百分比 · 速度
        if total > 0:
            percent = min(received * 100 // total, 100)
            text = f"{_fmt_size(received)} / {_fmt_size(total)}  ({percent}%)"
        else:
            text = f"已下载 {_fmt_size(received)}"
        if speed > 0:
            text += f"  ·  {_fmt_speed(speed)}"
        self.download_status.setText(text)

    def _on_download_finished(self, success: bool, message: str):
        # 区域常驻：仅隐藏进度条与状态文本
        self.download_progress.setVisible(False)
        self.download_progress_busy.stop()
        self.download_progress_busy.setVisible(False)
        self.download_status.setVisible(False)
        self._speed_samples = []
        # 结束态：隐藏暂停/取消按钮，重置暂停图标
        self.pause_btn.setIcon(FIF.PAUSE)
        self.pause_btn.setVisible(False)
        self.cancel_btn.setVisible(False)
        self.download_btn.setEnabled(self.asset_table.rowCount() > 0)
        if success:
            # 成功后在进度条原位显示"打开文件夹"入口
            if self._last_download_dir and os.path.isdir(self._last_download_dir):
                self.open_folder_btn.setVisible(True)
            from qfluentwidgets import InfoBar

            InfoBar.success("下载完成", message, parent=self)
        else:
            from qfluentwidgets import InfoBar

            InfoBar.error("下载失败", message[:150], parent=self)

    def _on_download_paused(self, received: int, total: int):
        """下载已暂停：暂停按钮切为恢复图标，状态文本提示可恢复。"""
        self.pause_btn.setIcon(FIF.PLAY)
        self.download_progress_busy.stop()
        self.download_progress_busy.setVisible(False)
        if total > 0:
            text = f"已暂停，点击 ▶ 恢复（{_fmt_size(received)} / {_fmt_size(total)}）"
        else:
            text = f"已暂停，点击 ▶ 恢复（已下载 {_fmt_size(received)}）"
        self.download_status.setText(text)

    def _toggle_pause(self):
        """暂停/恢复切换：下载中→暂停；已暂停→恢复。"""
        if self.download_manager.is_running:
            self.download_manager.pause()
        else:
            self.download_manager.resume()
            self.pause_btn.setIcon(FIF.PAUSE)  # 恢复下载后切回"暂停"图标

    def _cancel_download(self):
        """取消下载（删除半成品文件）。"""
        self.download_manager.cancel()

    def _open_download_folder(self):
        """在系统资源管理器中打开本次下载所在目录。"""
        if self._last_download_dir and os.path.isdir(self._last_download_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_download_dir))

    # ------------------------------------------------------------------
    # 关闭清理
    # ------------------------------------------------------------------
    def done(self, code):
        # 关窗时若仍在下载则取消（deno 脚本会清理半成品文件）
        self.download_manager.cancel()
        # 丢弃仍在执行的列表加载任务结果（future 由 TaskExecutor 保活至结束）
        if self._load_future is not None:
            self._load_future.detach()
            self._load_future = None
        super().done(code)
