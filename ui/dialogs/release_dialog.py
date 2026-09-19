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

from loguru import logger
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QTextCursor
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
)

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


# ---------------------------------------------------------------------------
# 发布说明 markdown → HTML（轻量转换：标题/粗体/斜体/行内代码/代码块/链接/列表）
# ---------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_inline(text: str) -> str:
    """行内元素转换（先转义，code 内不再处理其余格式）。"""
    text = _md_escape(text)
    # 行内代码用占位符保护，避免内部内容被链接/加粗规则二次处理
    code_spans: list[str] = []

    def _stash(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[图片]", text)  # 图片占位
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<i>\1</i>", text)
    for i, span in enumerate(code_spans):
        text = text.replace(f"\x00{i}\x00", f"<code>{span}</code>")
    return text


def _md_to_html(md: str) -> str:
    """Release body 常见 markdown 子集 → HTML（GitHub 风格近似）。"""
    html: list[str] = []
    code_buf: list[str] = []
    in_code = False
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    for raw in md.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            if in_code:
                html.append(
                    f"<pre>{_md_escape(chr(10).join(code_buf))}</pre>"
                )
                code_buf = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue

        if not line:
            close_list()
            continue

        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            close_list()
            level = min(len(m.group(1)) + 1, 5)  # h2 起步，避免标题过大
            html.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            continue

        if re.match(r"^[-*+]\s+", line):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_md_inline(re.sub(r'^[-*+]\\s+', '', line))}</li>")
            continue

        if re.match(r"^\d+[.)]\s+", line):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_md_inline(re.sub(r'^\\d+[.)]\\s+', '', line))}</li>")
            continue

        close_list()
        html.append(f"<p>{_md_inline(line)}</p>")

    close_list()
    if in_code and code_buf:
        html.append(f"<pre>{_md_escape(chr(10).join(code_buf))}</pre>")
    return "".join(html) or "<p>（无发布说明）</p>"


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

        # 左侧：版本列表（仿 GitHub tag 侧栏）
        self.tag_list = ListWidget()
        self.tag_list.setFixedWidth(200)
        self.tag_list.setMinimumHeight(470)
        self.tag_list.itemSelectionChanged.connect(self._on_release_selected)
        body.addWidget(self.tag_list)

        # 右侧：上部发布信息 + 下部资产表格
        right = QVBoxLayout()
        right.setSpacing(6)

        self.release_title = SubtitleLabel("")
        right.addWidget(self.release_title)

        self.release_meta = BodyLabel("")
        right.addWidget(self.release_meta)

        right.addWidget(HorizontalSeparator())

        self.body_view = TextBrowser()
        self.body_view.setOpenExternalLinks(True)
        self.body_view.setFixedHeight(180)
        self.body_view.document().setDefaultStyleSheet(
            "a { color: #4cc2ff; }"
            "code { background: rgba(255,255,255,26); }"
            "pre { background: rgba(255,255,255,20); }"
            "h2,h3,h4,h5 { color: rgba(255,255,255,228); }"
        )
        right.addWidget(self.body_view)

        right.addWidget(StrongBodyLabel("资产文件"))

        self.asset_table = TableWidget()
        self.asset_table.setColumnCount(4)
        self.asset_table.setHorizontalHeaderLabels(
            ["文件名", "大小", "日期", "下载次数"]
        )
        self.asset_table.verticalHeader().setVisible(False)
        self.asset_table.setFixedHeight(200)
        # 列宽分配：文件名列固定上限（超长省略+悬停看全名），其余列自适应内容，
        # 下载次数列弹性填充剩余空间
        self.asset_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive
        )
        self.asset_table.setColumnWidth(0, 300)
        self.asset_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.asset_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.asset_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.asset_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        right.addWidget(self.asset_table)

        body.addLayout(right)

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
        progress_layout.addStretch(1)

        # 列表加载：不确定进度条
        self.load_progress = IndeterminateProgressBar()
        self.load_progress.setVisible(False)
        # 资产下载：确定进度条
        self.download_progress = ProgressBar()
        self.download_progress.setVisible(False)
        self.download_status = BodyLabel("")
        self.download_status.setVisible(False)
        progress_layout.addWidget(self.load_progress)
        progress_layout.addWidget(self.download_progress)
        progress_layout.addWidget(self.download_status)
        progress_layout.addStretch(1)
        # 固定高度：空态（无加载/下载）区域依然完整保留
        self.progress_area.setFixedHeight(64)
        self.viewLayout.addWidget(self.progress_area)

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

        self._load_future = (
            TaskExecutor.run(fetch_release_list, remote_url, token, proxy)
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
            date = rel["published_at"] or "未发布"
            sub = " · ".join([date, *badges]) if badges else date
            item = QListWidgetItem(f"{tag}\n{sub}")
            item.setSizeHint(QSize(180, 46))
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
            self.asset_table.setItem(
                row, 3, QTableWidgetItem(str(count) if count is not None else "-")
            )
            self.asset_table.item(row, 0).setData(Qt.UserRole, asset)
        self.download_btn.setEnabled(bool(assets))

    # ------------------------------------------------------------------
    # 资产下载
    # ------------------------------------------------------------------
    @staticmethod
    def _unique_save_path(base: str, name: str) -> str:
        """在目录内生成不冲突的保存路径：已存在时追加 (1)、(2)… 序号。"""
        candidate = os.path.join(base, name)
        if not os.path.exists(candidate):
            return candidate
        stem, ext = os.path.splitext(name)
        for i in range(1, 1000):
            candidate = os.path.join(base, f"{stem} ({i}){ext}")
            if not os.path.exists(candidate):
                return candidate
        return os.path.join(base, name)  # 极端情况：原样返回

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
        self.download_progress.setVisible(True)
        self.download_progress.setValue(0)
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
            self.download_progress.setMaximum(total)
            self.download_progress.setValue(min(received, total))
        else:
            # 未知大小：显示不确定进度
            self.download_progress.setMaximum(0)

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
        self.download_status.setVisible(False)
        self._speed_samples = []
        self.download_btn.setEnabled(self.asset_table.rowCount() > 0)
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
        # 丢弃仍在执行的列表加载任务结果（future 由 TaskExecutor 保活至结束）
        if self._load_future is not None:
            self._load_future.detach()
            self._load_future = None
        super().done(code)
