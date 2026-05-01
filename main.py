import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from github_release_downloader import (
    AuthSession,
    GitHubRepo as DownloaderGitHubRepo,
    ReleaseAsset as DownloaderReleaseAsset,
    download_asset as downloader_download_asset,
    get_assets as downloader_get_assets,
    get_available_versions as downloader_get_available_versions,
)
from loguru import logger
from semantic_version import Version
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    HorizontalSeparator,
    InfoBar,
    LineEdit,
    ListWidget,
    MessageBox,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TextEdit,
    Theme,
    ToolButton,
    setTheme,
)
from qfluentwidgets import FluentIcon as FIF

from res_rc import qInitResources

logger.remove()
logger.add("git_manager_{time:YYYY-MM-DD}.log", rotation="10 MB", retention="7 days", encoding="utf-8")
CONFIG_FILE = Path(__file__).with_name("config.json")
RELEASE_CACHE_FILE = Path(__file__).with_name("release_cache.json")
MAX_DIFF_BYTES = 1024 * 1024
RELEASE_CACHE_TTL_SECONDS = 15 * 60


def load_config() -> dict:
    try:
        if not CONFIG_FILE.exists():
            return {}
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载配置失败: {str(e)}")
        return {}


def save_config(config: dict):
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存配置失败: {str(e)}")


def saved_github_token() -> str:
    return str(load_config().get("github_token") or "").strip()


class RateLimiter:
    def __init__(self, min_interval_seconds: float = 1.0):
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, key: str):
        with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed.get(key, now)
            delay = max(0.0, next_allowed - now)
            self._next_allowed[key] = max(now, next_allowed) + self.min_interval_seconds

        if delay:
            time.sleep(delay)


class ReleaseCache:
    def __init__(self, filename: Path = RELEASE_CACHE_FILE, ttl_seconds: int = RELEASE_CACHE_TTL_SECONDS):
        self.filename = filename
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    @staticmethod
    def key(source: "ReleaseSource") -> str:
        return f"{source.host}/{source.owner}/{source.repo}"

    def get(self, source: "ReleaseSource") -> list[dict] | None:
        with self._lock:
            cache = self._load()
            entry = cache.get(self.key(source))
            if not entry:
                return None

            if time.time() - float(entry.get("saved_at") or 0) > self.ttl_seconds:
                return None

            releases = entry.get("releases")
            if not isinstance(releases, list):
                return None

            return [self._from_cache_release(item) for item in releases if isinstance(item, dict)]

    def set(self, source: "ReleaseSource", releases: list[dict]):
        with self._lock:
            cache = self._load()
            cache[self.key(source)] = {
                "saved_at": time.time(),
                "releases": [self._to_cache_release(release) for release in releases],
            }
            self._save(cache)

    def _load(self) -> dict:
        try:
            if not self.filename.exists():
                return {}
            with self.filename.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"加载 Release 缓存失败: {str(e)}")
            return {}

    def _save(self, cache: dict):
        try:
            with self.filename.open("w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存 Release 缓存失败: {str(e)}")

    @staticmethod
    def _to_cache_release(release: dict) -> dict:
        cached = dict(release)
        source = cached.get("source")
        if isinstance(source, ReleaseSource):
            cached["source"] = source.__dict__
        return cached

    @staticmethod
    def _from_cache_release(release: dict) -> dict:
        cached = dict(release)
        source = cached.get("source")
        if isinstance(source, dict):
            cached["source"] = ReleaseSource(**source)
        return cached


release_rate_limiter = RateLimiter()
release_cache = ReleaseCache()


def build_hidden_subprocess_kwargs() -> dict:
    kwargs = {}
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        kwargs["startupinfo"] = startupinfo
    return kwargs


def run_hidden(cmd, **kwargs):
    hidden_kwargs = build_hidden_subprocess_kwargs()
    for key, value in hidden_kwargs.items():
        kwargs.setdefault(key, value)
    return subprocess.run(cmd, **kwargs)


@dataclass
class GitRepoInfo:
    url: str


@dataclass
class GitRepoCandidate:
    name: str
    path: str
    is_git: bool
    git_path: str


@dataclass
class ReleaseSource:
    host: str
    owner: str
    repo: str
    api_url: str


def derive_repo_name(repo_input: str | GitRepoInfo) -> str:
    url = repo_input.url if isinstance(repo_input, GitRepoInfo) else str(repo_input)
    cleaned = url.strip().rstrip("/").rstrip("\\")
    if not cleaned:
        return ""

    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    repo_name = os.path.basename(cleaned.replace(":", "/"))
    return repo_name.strip()


def build_clone_candidates(repo_input: str | GitRepoInfo) -> list[str]:
    url = repo_input.url if isinstance(repo_input, GitRepoInfo) else str(repo_input)
    primary = url.strip()
    if not primary:
        return []

    candidates = [primary]
    parsed = urlparse(primary)

    if parsed.scheme in ("http", "https") and parsed.netloc.lower() == "github.com":
        candidates.append(f"https://ghproxy.com/{primary}")
    elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitee.com":
        candidates.insert(0, primary)
    elif parsed.scheme in ("http", "https") and parsed.netloc.lower() == "gitlab.com":
        candidates.append(primary)

    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def parse_release_source(remote_url: str) -> ReleaseSource | None:
    url = remote_url.strip()
    if not url:
        return None

    if url.startswith("git@") and ":" in url:
        host_part, path_part = url[4:].split(":", 1)
        host = host_part.lower()
        path = path_part
    else:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        path = parsed.path.lstrip("/")

    if host.startswith("www."):
        host = host[4:]

    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = parts[1]

    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    elif host == "gitee.com":
        api_url = f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases"
    elif host == "gitlab.com":
        project = quote(f"{owner}/{repo}", safe="")
        api_url = f"https://gitlab.com/api/v4/projects/{project}/releases"
    else:
        return None

    return ReleaseSource(host=host, owner=owner, repo=repo, api_url=api_url)


def human_file_size(size: int | str | None) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "N/A"

    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

    return "N/A"


def release_total_size(release: dict) -> int | None:
    total = 0
    has_size = False
    for asset in release.get("assets") or []:
        try:
            total += int(asset.get("size") or 0)
            has_size = True
        except (TypeError, ValueError):
            continue
    return total if has_size else None


def format_release_summary(release: dict) -> str:
    return release.get("tag") or release.get("name") or "Release"


def sanitize_download_filename(name: str) -> str:
    filename = os.path.basename(name).strip() or "release-asset"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ") or "release-asset"


def limit_text_bytes(text: str, max_bytes: int = MAX_DIFF_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False

    notice = f"\n\n--- Diff 内容超过 {human_file_size(max_bytes)}，已截断显示 ---"
    notice_bytes = notice.encode("utf-8", errors="replace")
    content_limit = max(0, max_bytes - len(notice_bytes))
    limited = encoded[:content_limit].decode("utf-8", errors="ignore")
    return limited + notice, True


def normalize_release(raw: dict, source: ReleaseSource) -> dict:
    tag = raw.get("tag_name") or raw.get("tagName") or raw.get("tag") or ""
    name = raw.get("name") or tag or "未命名 Release"
    published_at = (
        raw.get("published_at") or raw.get("released_at") or raw.get("created_at") or raw.get("updated_at") or ""
    )
    body = raw.get("body") or raw.get("description") or ""
    html_url = raw.get("html_url") or raw.get("_links", {}).get("self") or raw.get("url") or ""

    assets = []
    raw_assets = raw.get("assets") or []
    asset_items = raw_assets if isinstance(raw_assets, list) else []
    for asset in asset_items:
        download_url = (
            asset.get("browser_download_url")
            or asset.get("direct_asset_url")
            or asset.get("url")
            or asset.get("download_url")
        )
        if not download_url:
            continue
        asset_name = asset.get("name") or os.path.basename(urlparse(download_url).path) or "release-asset"
        assets.append(
            {
                "name": asset_name,
                "size": asset.get("size"),
                "url": download_url,
            }
        )

    gitlab_links = ((raw.get("assets") or {}).get("links") or []) if isinstance(raw.get("assets"), dict) else []
    for link in gitlab_links:
        download_url = link.get("direct_asset_url") or link.get("url")
        if not download_url:
            continue
        assets.append(
            {
                "name": link.get("name") or os.path.basename(urlparse(download_url).path) or "release-asset",
                "size": link.get("size"),
                "url": download_url,
            }
        )

    for key, label in (("zipball_url", "Source code (zip)"), ("tarball_url", "Source code (tar.gz)")):
        if raw.get(key):
            assets.append({"name": f"{tag or name} - {label}", "size": None, "url": raw[key]})

    for source_item in raw.get("sources") or []:
        download_url = source_item.get("url")
        if not download_url:
            continue
        assets.append(
            {
                "name": source_item.get("format") or os.path.basename(urlparse(download_url).path) or "source",
                "size": None,
                "url": download_url,
            }
        )

    return {
        "tag": tag,
        "name": name,
        "published_at": published_at,
        "body": body,
        "html_url": html_url,
        "source": source,
        "assets": assets,
    }


def build_release_request_headers(binary: bool = False) -> dict:
    headers = {
        "Accept": "application/octet-stream" if binary else "application/json",
        "User-Agent": "GitManager/1.0",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or saved_github_token()


def sync_github_downloader_auth(token: str):
    AuthSession.header = {"Authorization": f"Bearer {token}"} if token else {}


def parse_github_release_tag(tag_name: str) -> Version:
    cleaned = tag_name.strip().lstrip("vV")
    match = re.search(r"\d+(?:\.\d+){0,2}", cleaned)
    if not match:
        return Version("0.0.0")

    parts = match.group(0).split(".")
    while len(parts) < 3:
        parts.append("0")

    return Version(".".join(parts[:3]))


def classify_release_error(status_code: int | None = None, detail: str = "", exc: Exception | None = None) -> str:
    if status_code in (401, 403):
        if "rate limit" in detail.lower():
            return "限流"
        return "认证"
    if status_code == 404:
        return "不存在"
    if status_code and status_code >= 500:
        return "服务端"
    if isinstance(exc, urllib.error.URLError):
        return "网络"
    if "Invalid version string" in detail:
        return "版本解析"
    return "未知"


def build_tag_release_fallback(source: ReleaseSource, tag: str) -> dict:
    if source.host == "github.com":
        html_url = f"https://github.com/{source.owner}/{source.repo}/releases/tag/{tag}"
        assets = [
            {
                "name": f"{tag} - Source code (zip)",
                "size": None,
                "url": f"https://github.com/{source.owner}/{source.repo}/archive/refs/tags/{tag}.zip",
            },
            {
                "name": f"{tag} - Source code (tar.gz)",
                "size": None,
                "url": f"https://github.com/{source.owner}/{source.repo}/archive/refs/tags/{tag}.tar.gz",
            },
        ]
    elif source.host == "gitee.com":
        html_url = f"https://gitee.com/{source.owner}/{source.repo}/releases/tag/{tag}"
        assets = [
            {
                "name": f"{tag} - Source code (zip)",
                "size": None,
                "url": f"https://gitee.com/{source.owner}/{source.repo}/repository/archive/{tag}.zip",
            },
            {
                "name": f"{tag} - Source code (tar.gz)",
                "size": None,
                "url": f"https://gitee.com/{source.owner}/{source.repo}/repository/archive/{tag}.tar.gz",
            },
        ]
    else:
        html_url = f"https://gitlab.com/{source.owner}/{source.repo}/-/releases/{tag}"
        assets = [
            {
                "name": f"{tag} - Source code (zip)",
                "size": None,
                "url": f"https://gitlab.com/{source.owner}/{source.repo}/-/archive/{tag}/{source.repo}-{tag}.zip",
            },
            {
                "name": f"{tag} - Source code (tar.gz)",
                "size": None,
                "url": f"https://gitlab.com/{source.owner}/{source.repo}/-/archive/{tag}/{source.repo}-{tag}.tar.gz",
            },
        ]

    return {
        "tag": tag,
        "name": tag,
        "published_at": "API 不可用，来自远程 tag",
        "body": "Release API 查询失败，已退回到远程 tag 列表。此模式只能下载源码包，无法显示 Release 附件和正文。",
        "html_url": html_url,
        "source": source,
        "assets": assets,
    }


def safe_remove_repo_dir(base_path: str, repo_name: str, onexc=None) -> dict:
    result = {"success": False, "error": None}

    if not repo_name or not repo_name.strip():
        result["error"] = "repo_name 不能为空"
        return result

    if os.path.sep in repo_name or (os.path.altsep and os.path.altsep in repo_name):
        result["error"] = "repo_name 不能包含路径分隔符"
        return result

    if repo_name in (".", "..") or ".." in repo_name:
        result["error"] = "repo_name 包含非法路径片段"
        return result

    base_abs = os.path.abspath(base_path)
    target_abs = os.path.abspath(os.path.join(base_abs, repo_name))

    if not os.path.exists(base_abs):
        result["error"] = "base_path 不存在"
        return result

    if not os.path.exists(target_abs):
        result["error"] = "target_path 不存在"
        return result

    if base_abs == target_abs:
        result["error"] = "禁止删除 base_path 本身"
        return result

    base_prefix = base_abs.rstrip("\\/") + os.sep
    if not target_abs.startswith(base_prefix):
        result["error"] = "target_path 不在 base_path 子目录中"
        return result

    try:
        shutil.rmtree(target_abs, onexc=onexc)
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def scan_git_repos(base_path: str) -> list[GitRepoCandidate]:
    if not base_path or not base_path.strip():
        return []

    if ".." in Path(base_path).parts:
        return []

    base_abs = os.path.abspath(base_path)
    if not os.path.isdir(base_abs):
        return []

    candidates: list[GitRepoCandidate] = []
    base_prefix = base_abs.rstrip("\\/") + os.sep

    try:
        for entry_name in os.listdir(base_abs):
            child_path = os.path.abspath(os.path.join(base_abs, entry_name))

            if not child_path.startswith(base_prefix):
                continue

            if not os.path.isdir(child_path):
                continue

            git_path = os.path.abspath(os.path.join(child_path, ".git"))
            if not git_path.startswith(child_path.rstrip("\\/") + os.sep):
                continue

            if os.path.exists(git_path) and os.path.isdir(git_path):
                candidates.append(
                    GitRepoCandidate(
                        name=entry_name,
                        path=child_path,
                        is_git=True,
                        git_path=git_path,
                    )
                )
    except OSError:
        return []

    return candidates


class DiffDialog(QDialog):
    def __init__(self, repo: str, sections: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Diff - {repo}")
        self.resize(900, 600)

        layout = QHBoxLayout(self)

        # 左侧导航
        self.nav = ListWidget()
        for key in sections.keys():
            self.nav.addItem(key)

        # 右侧内容
        self.viewer = TextEdit()
        self.viewer.setReadOnly(True)
        # self.viewer.setFont(QFont("Consolas", 10))

        layout.addWidget(self.nav, 1)
        layout.addWidget(self.viewer, 4)

        self.sections = sections
        self.nav.currentTextChanged.connect(self.update_view)

        # 默认选中第一个
        if sections:
            self.nav.setCurrentRow(0)

    def update_view(self, key):
        text = self.sections.get(key, "")
        self.viewer.setHtml(self.format_diff_html(text))

    @staticmethod
    def format_diff_html(text: str) -> str:
        lines = []

        for raw_line in text.splitlines():
            escaped = html.escape(raw_line)

            if raw_line.startswith("+++") or raw_line.startswith("---"):
                color = "#8e8e93"
            elif raw_line.startswith("+"):
                color = "#16a34a"
            elif raw_line.startswith("-"):
                color = "#dc2626"
            elif raw_line.startswith("@@"):
                color = "#2563eb"
            else:
                color = "#f5f5f5"

            lines.append(f'<span style="color: {color};">{escaped}</span>')

        return "<br>".join(lines) if lines else ""


class CloneRepoDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("克隆仓库")
        self.urlLineEdit = LineEdit()

        self.urlLineEdit.setPlaceholderText("输入 Git 仓库链接")
        self.urlLineEdit.setClearButtonEnabled(True)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.urlLineEdit)

        # ========== 新增：Git 链接实时验证 ==========
        self.urlLineEdit.textChanged.connect(self._validate_url)
        self.yesButton.setEnabled(False)  # 初始禁用“克隆”按钮
        # ===========================================

        self.widget.setMinimumWidth(420)
        self.yesButton.setText("克隆")
        self.cancelButton.setText("取消")

    def repo_url(self) -> str:
        return self.urlLineEdit.text().strip()

    # ========== 新增：Git 链接验证方法 ==========
    def _validate_url(self):
        """实时验证输入的 Git 仓库链接"""
        url = self.urlLineEdit.text().strip()
        if self._is_valid_git_url(url):
            self.yesButton.setEnabled(True)
            # 恢复正常样式（如果之前有错误提示）
            self.urlLineEdit.setStyleSheet("")
        else:
            self.yesButton.setEnabled(False)
            # 可选：添加红色边框提示错误（qfluentwidgets 的 LineEdit 支持）
            # self.urlLineEdit.setStyleSheet("border: 1px solid #e74c3c;")

    @staticmethod
    def _is_valid_git_url(url: str) -> bool:
        """Git 仓库链接验证（支持 HTTPS、SSH、Git 协议等常见格式）"""
        if not url:
            return False

        # 推荐的 Git URL 正则（经过实际项目验证，能覆盖绝大部分合法场景）
        pattern = re.compile(
            r'^(?:(?:https?|git|ssh)://'  # 协议
            r'(?:[^@]+@)?'  # 可选用户名
            r'[\w.-]+'  # 主机名
            r'(?::\d+)?'  # 可选端口
            r'[:/]'  # 分隔符
            r'[\w./-]+?'  # 路径
            r'(?:\.git)?/?)$',  # 可选 .git 后缀
            re.IGNORECASE,
        )
        return bool(pattern.match(url))


class QtLogHandler(QObject):
    log_signal = Signal(str)

    def __init__(self, text_edit):
        super().__init__()
        self.text_edit = text_edit
        self.log_signal.connect(self._append_to_ui)

    def write(self, message: str):
        self.log_signal.emit(message)

    def _append_to_ui(self, message: str):
        self.text_edit.append(message.rstrip())
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.text_edit.setTextCursor(cursor)


# ====================== 版本历史弹窗 ======================
class HistoryDialog(QDialog):
    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.setWindowTitle(f"版本历史 - {os.path.basename(repo_path)}")
        self.resize(980, 680)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Commit", "日期", "作者", "提交信息"])
        self.table.verticalHeader().setVisible(False)  # 去掉左侧序号列

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换版本")
        self.switch_btn.clicked.connect(self.switch_to_version)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_history()

    def load_history(self):
        try:
            cmd = ["git", "log", "--pretty=format:%H|%ad|%an|%s", "--date=format:%Y-%m-%d %H:%M", "-30"]

            result = run_hidden(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                return

            self.table.setRowCount(0)
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                commit, date, author, message = line.split('|', 3)

                item_commit = QTableWidgetItem(commit[:12])  # 显示短 hash
                item_commit.setData(Qt.UserRole, commit)
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, item_commit)
                self.table.setItem(row, 1, QTableWidgetItem(date))
                self.table.setItem(row, 2, QTableWidgetItem(author))
                self.table.setItem(row, 3, QTableWidgetItem(message))

                if row == 0:
                    for col in range(4):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(0, 120, 215, 40))
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
        except Exception as e:
            logger.error(f"加载历史失败: {str(e)}")

    def on_double_click(self, item):
        self.switch_to_version()

    def switch_to_version(self):
        selected = self.table.selectedItems()
        if not selected:
            return

        row = selected[0].row()

        item = self.table.item(row, 0)

        # ✅ 优先用完整 commit
        commit = item.data(Qt.UserRole)
        if not commit:
            commit = item.text()  # fallback（防止旧数据）

        box = MessageBox(
            "确认切换版本", f"确定要切换到该版本？\n\nCommit: {commit[:12]}\n\n⚠ 此操作会丢弃所有未提交更改！", self
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_commit(self.repo_path, commit, self)


# ====================== 分支列表弹窗 ======================
class BranchDialog(QDialog):
    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.setWindowTitle(f"分支管理 - {os.path.basename(repo_path)}")
        self.resize(860, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["分支", "类型", "当前", "最新提交", "提交信息"])
        self.table.verticalHeader().setVisible(False)

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.switch_btn = PrimaryPushButton(FIF.UPDATE, "切换分支")
        self.switch_btn.clicked.connect(self.switch_to_branch)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.switch_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_branches()

    def load_branches(self):
        try:
            run_hidden(
                ["git", "fetch", "--quiet"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            current_result = run_hidden(
                ["git", "branch", "--show-current"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            current_branch = current_result.stdout.strip() if current_result.returncode == 0 else ""

            result = run_hidden(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(refname)|%(refname:short)|%(objectname:short)|%(committerdate:format:%Y-%m-%d %H:%M)|%(subject)",
                    "refs/heads",
                    "refs/remotes",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                return

            self.table.setRowCount(0)
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue

                refname, short_name, commit, date, message = line.split("|", 4)
                if refname.startswith("refs/remotes/") and refname.endswith("/HEAD"):
                    continue

                is_remote = refname.startswith("refs/remotes/")
                branch_type = "远程" if is_remote else "本地"
                is_current = not is_remote and short_name == current_branch

                branch_item = QTableWidgetItem(short_name)
                branch_item.setData(
                    Qt.UserRole,
                    {
                        "name": short_name,
                        "refname": refname,
                        "is_remote": is_remote,
                    },
                )

                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, branch_item)
                self.table.setItem(row, 1, QTableWidgetItem(branch_type))
                self.table.setItem(row, 2, QTableWidgetItem("✓" if is_current else ""))
                self.table.setItem(row, 3, QTableWidgetItem(f"{commit}  {date}".strip()))
                self.table.setItem(row, 4, QTableWidgetItem(message))

                if is_current:
                    for col in range(5):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(0, 120, 215, 40))
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
        except Exception as e:
            logger.error(f"加载分支失败: {str(e)}")

    def on_double_click(self, item):
        self.switch_to_branch()

    def switch_to_branch(self):
        selected = self.table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        item = self.table.item(row, 0)
        branch_info = item.data(Qt.UserRole) if item else None
        if not branch_info:
            return

        branch_name = branch_info["name"]
        branch_type = "远程分支" if branch_info["is_remote"] else "本地分支"

        box = MessageBox(
            "确认切换分支",
            f"确定要切换到该{branch_type}？\n\n分支: {branch_name}\n\n未提交的更改可能导致切换失败，请先确认工作区状态。",
            self,
        )

        box.yesButton.setText("切换")
        box.cancelButton.setText("取消")
        box.cancelButton.setFocus()

        if box.exec():
            self.parent().switch_to_branch(self.repo_path, branch_info, self)


# ====================== Release 信息弹窗 ======================
class ReleaseDialog(QDialog):
    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)
        self.repo_path = repo_path
        self.releases: list[dict] = []
        self.current_assets: list[dict] = []
        self.setWindowTitle(f"Release - {os.path.basename(repo_path)}")
        self.resize(1040, 700)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(StrongBodyLabel(f"仓库: {repo_path}"))

        splitter = QSplitter(Qt.Vertical)

        self.release_table = TableWidget()
        self.release_table.setColumnCount(2)
        self.release_table.setHorizontalHeaderLabels(["发布版本号", "大小"])
        self.release_table.verticalHeader().setVisible(False)
        self.release_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.release_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.release_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.release_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.release_table.itemSelectionChanged.connect(self.update_release_detail)
        splitter.addWidget(self.release_table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)

        detail_layout.addWidget(StrongBodyLabel("Release 信息"))
        self.detail_text = TextEdit()
        self.detail_text.setReadOnly(True)
        detail_layout.addWidget(self.detail_text, 2)

        detail_layout.addWidget(StrongBodyLabel("可下载资源"))
        self.asset_table = TableWidget()
        self.asset_table.setColumnCount(3)
        self.asset_table.setHorizontalHeaderLabels(["文件", "大小", "地址"])
        self.asset_table.verticalHeader().setVisible(False)
        self.asset_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.asset_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.asset_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.asset_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        detail_layout.addWidget(self.asset_table, 1)

        splitter.addWidget(detail_widget)
        splitter.setSizes([260, 420])
        layout.addWidget(splitter, 1)

        btn_layout = QHBoxLayout()
        self.refresh_btn = PushButton(FIF.SYNC, "刷新")
        self.refresh_btn.clicked.connect(self.load_releases)
        self.download_btn = PrimaryPushButton(FIF.UPDATE, "下载选中资源")
        self.download_btn.clicked.connect(self.download_selected_asset)
        self.close_btn = PushButton(FIF.CLOSE, "关闭窗口")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.download_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.load_releases()

    def load_releases(self):
        self.release_table.setRowCount(0)
        self.asset_table.setRowCount(0)
        self.detail_text.clear()
        self.current_assets = []
        self.releases = []

        releases, error = self.parent().fetch_releases(self.repo_path)
        if error:
            self.detail_text.setPlainText(error)
            self.download_btn.setEnabled(False)
            return

        self.releases = releases
        if not releases:
            self.detail_text.setPlainText("当前仓库暂无 Release。")
            self.download_btn.setEnabled(False)
            return

        for release in releases:
            row = self.release_table.rowCount()
            self.release_table.insertRow(row)

            tag_item = QTableWidgetItem(release["tag"])
            tag_item.setData(Qt.UserRole, release)
            self.release_table.setItem(row, 0, tag_item)
            total_size = release_total_size(release)
            self.release_table.setItem(
                row, 1, QTableWidgetItem(human_file_size(total_size) if total_size is not None else "N/A")
            )

        self.release_table.setCurrentCell(0, 0)
        self.download_btn.setEnabled(True)

    def update_release_detail(self):
        selected = self.release_table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        release = self.release_table.item(row, 0).data(Qt.UserRole)
        if not release:
            return

        source = release["source"]
        detail = [
            f"平台: {source.host}",
            f"项目: {source.owner}/{source.repo}",
            f"Tag: {release['tag']}",
            f"名称: {release['name']}",
            f"发布时间: {release['published_at']}",
            f"链接: {release['html_url']}",
            "",
            release["body"] or "无说明。",
        ]
        self.detail_text.setPlainText("\n".join(detail))

        self.current_assets = release["assets"]
        self.asset_table.setRowCount(0)
        for asset in self.current_assets:
            row = self.asset_table.rowCount()
            self.asset_table.insertRow(row)

            name_item = QTableWidgetItem(asset["name"])
            name_item.setData(Qt.UserRole, asset)
            self.asset_table.setItem(row, 0, name_item)
            self.asset_table.setItem(row, 1, QTableWidgetItem(human_file_size(asset.get("size"))))
            self.asset_table.setItem(row, 2, QTableWidgetItem(asset["url"]))

        self.download_btn.setEnabled(bool(self.current_assets))

    def download_selected_asset(self):
        selected = self.asset_table.selectedItems()
        if not selected:
            InfoBar.warning("提示", "请先选中一个下载资源", parent=self)
            return

        row = selected[0].row()
        asset = self.asset_table.item(row, 0).data(Qt.UserRole)
        if not asset:
            return

        target_dir = QFileDialog.getExistingDirectory(self, "选择下载目录", self.repo_path)
        if not target_dir:
            return

        self.parent().download_release_asset(self.repo_path, asset, target_dir)


# ====================== 主窗口 ======================
class GitManager(QMainWindow):
    update_row_signal = Signal(
        int, str, str, str, str, str, str, str
    )  # row, repo, branch, local, remote, status, ahead_behind, release
    notify_signal = Signal(str, str, str)
    refresh_repos_signal = Signal()

    def __init__(self):
        super().__init__()
        qInitResources()
        setTheme(Theme.LIGHT)
        run_hidden(["git", "config", "--global", "core.quotepath", "false"], check=False)

        self.setWindowTitle("Git 多仓库管理器 - 高级版")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(1280, 800)

        self.base_dir = self.load_base_dir()
        self.repos: list[str] = []
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._is_closing = False
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()

        self.init_ui()

        self.qt_handler = QtLogHandler(self.log_text)
        logger.add(
            self.qt_handler.write, level="DEBUG", format="{time:HH:mm:ss} | <level>{level:8}</level> | {message}"
        )
        logger.success("Git 多仓库管理器启动成功")

    def closeEvent(self, event):
        self._is_closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self._terminate_active_processes()
        logger.remove()
        super().closeEvent(event)

    def load_base_dir(self) -> str:
        default_dir = os.getcwd()
        try:
            config = load_config()
            saved_dir = str(config.get("base_dir") or "").strip()
            if saved_dir and os.path.isdir(saved_dir):
                return saved_dir
        except Exception as e:
            logger.warning(f"加载配置失败: {str(e)}")

        return default_dir

    def save_base_dir(self):
        try:
            config = load_config()
            config["base_dir"] = self.base_dir
            save_config(config)
        except Exception as e:
            logger.warning(f"保存配置失败: {str(e)}")

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 顶部
        top = QHBoxLayout()
        self.dir_label = StrongBodyLabel(f"目录: {self.base_dir}")
        top.addWidget(self.dir_label)
        top.addStretch()

        self.select_btn = PushButton(FIF.FOLDER, "选择目录")
        self.select_btn.clicked.connect(self.select_folder)
        self.scan_btn = PushButton(FIF.SYNC, "扫描仓库")
        self.scan_btn.clicked.connect(self.scan_repos)
        self.add_repo_btn = PushButton(FIF.ADD, "添加仓库")
        self.add_repo_btn.clicked.connect(self.show_clone_dialog)
        self.remove_repo_btn = PushButton(FIF.DELETE, "删除仓库")
        self.remove_repo_btn.clicked.connect(self.delete_selected_repo)

        top.addWidget(self.select_btn)
        top.addWidget(self.scan_btn)
        top.addWidget(self.add_repo_btn)
        top.addWidget(self.remove_repo_btn)
        layout.addLayout(top)

        # 代理
        proxy_layout = QHBoxLayout()
        proxy_layout.addWidget(BodyLabel("代理:"))
        self.proxy_entry = LineEdit()
        self.proxy_entry.setText("http://127.0.0.1:7897")
        self.proxy_entry.setMinimumWidth(340)
        proxy_layout.addWidget(self.proxy_entry)

        apply_btn = PushButton(FIF.SEND, "应用代理")
        apply_btn.clicked.connect(self.apply_proxy)
        clear_btn = PushButton(FIF.CLEAR_SELECTION, "清除代理")
        clear_btn.clicked.connect(self.clear_proxy)
        proxy_layout.addWidget(apply_btn)
        proxy_layout.addWidget(clear_btn)
        proxy_layout.addStretch()
        layout.addLayout(proxy_layout)

        # GitHub Token
        token_layout = QHBoxLayout()
        token_layout.addWidget(BodyLabel("令牌:"))
        self.github_token_entry = LineEdit()
        self.github_token_entry.setPlaceholderText("输入 GitHub Token，用于提高 Release 查询额度")
        self.github_token_entry.setText(saved_github_token())
        self.github_token_entry.setEchoMode(LineEdit.Password)
        self.github_token_entry.setClearButtonEnabled(True)
        self.github_token_entry.setMinimumWidth(420)
        token_layout.addWidget(self.github_token_entry)

        validate_token_btn = PushButton(FIF.SEND, "验证令牌")
        validate_token_btn.clicked.connect(self.validate_github_token)
        clear_token_btn = PushButton(FIF.CLEAR_SELECTION, "清除令牌")
        clear_token_btn.clicked.connect(self.clear_github_token)
        token_layout.addWidget(validate_token_btn)
        token_layout.addWidget(clear_token_btn)
        token_layout.addStretch()
        layout.addLayout(token_layout)

        layout.addWidget(HorizontalSeparator())

        # 表格
        self.table = TableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["仓库名称", "当前分支", "当前版本", "最新版本", "状态 / 同步", "Release", "操作"]
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)

        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 170)
        self.table.setColumnWidth(5, 100)
        self.table.setColumnWidth(6, 90)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.itemClicked.connect(self.on_item_clicked)

        layout.addWidget(self.table, 1)

        # 日志
        log_layout = QVBoxLayout()
        log_layout.addWidget(StrongBodyLabel("运行日志"))
        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        layout.addLayout(log_layout, 1)

        self.update_row_signal.connect(self.update_table_row)
        self.notify_signal.connect(self.show_notification)
        self.refresh_repos_signal.connect(self.scan_repos)

    # ====================== 工具方法 ======================
    def log_print(self, msg: str):
        logger.info(msg)

    def show_notification(self, level: str, title: str, content: str):
        if level == "success":
            InfoBar.success(title, content, parent=self)
        elif level == "error":
            InfoBar.error(title, content, parent=self)
        else:
            InfoBar.warning(title, content, parent=self)

    def run_git(self, path: str, args: list, timeout=60):
        return self.run_command(["git"] + args, cwd=path, timeout=timeout)

    def fetch_releases(self, repo: str) -> tuple[list[dict], str | None]:
        remote_url, err, code = self.run_git(repo, ["remote", "get-url", "origin"], timeout=15)
        if code != 0 or not remote_url:
            return [], (err or "未找到 origin 远程仓库地址")

        source = parse_release_source(remote_url)
        if not source:
            return [], f"暂不支持该远程仓库的 Release 查询: {remote_url}"

        cached = release_cache.get(source)
        if cached is not None:
            logger.info(f"Release 使用缓存: {source.host}/{source.owner}/{source.repo}")
            return cached, None

        if source.host == "github.com":
            releases, error = self.fetch_github_releases_with_downloader(repo, source)
            if releases or error:
                if releases:
                    release_cache.set(source, releases)
                return releases, error

        try:
            release_rate_limiter.wait(source.host)
            request = urllib.request.Request(
                source.api_url,
                headers=build_release_request_headers(),
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8", errors="replace")
            raw_releases = json.loads(payload)
            if isinstance(raw_releases, dict) and raw_releases.get("message"):
                return [], str(raw_releases.get("message"))
            if not isinstance(raw_releases, list):
                return [], "Release API 返回格式异常"

            releases = [normalize_release(item, source) for item in raw_releases[:30] if isinstance(item, dict)]
            release_cache.set(source, releases)
            return releases, None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            fallback = self.fetch_tag_release_fallback(repo, source)
            if fallback:
                error_type = classify_release_error(e.code, detail)
                logger.warning(f"Release API 查询失败[{error_type}]，已使用 tag 兜底: HTTP {e.code} {detail}")
                release_cache.set(source, fallback)
                return fallback, None

            token_tip = ""
            if (
                source.host == "github.com"
                and e.code in (401, 403)
                and not github_token()
            ):
                token_tip = "\n\n可设置环境变量 GITHUB_TOKEN 或 GH_TOKEN 提高 GitHub API 额度。"
            error_type = classify_release_error(e.code, detail)
            return [], f"Release 查询失败[{error_type}]: HTTP {e.code} {detail}{token_tip}"
        except urllib.error.URLError as e:
            fallback = self.fetch_tag_release_fallback(repo, source)
            if fallback:
                error_type = classify_release_error(exc=e)
                logger.warning(f"Release API 查询失败[{error_type}]，已使用 tag 兜底: {e.reason}")
                release_cache.set(source, fallback)
                return fallback, None
            error_type = classify_release_error(exc=e)
            return [], f"Release 查询失败[{error_type}]: {e.reason}"
        except Exception as e:
            fallback = self.fetch_tag_release_fallback(repo, source)
            if fallback:
                error_type = classify_release_error(detail=str(e), exc=e)
                logger.warning(f"Release API 查询失败[{error_type}]，已使用 tag 兜底: {str(e)}")
                release_cache.set(source, fallback)
                return fallback, None
            error_type = classify_release_error(detail=str(e), exc=e)
            return [], f"Release 查询失败[{error_type}]: {str(e)}"

    def fetch_github_releases_with_downloader(
        self, repo_path: str, source: ReleaseSource
    ) -> tuple[list[dict], str | None]:
        downloader_repo = DownloaderGitHubRepo(source.owner, source.repo, github_token())
        AuthSession.init(downloader_repo)

        try:
            versions = list(downloader_get_available_versions(downloader_repo))[-30:][::-1]
        except Exception as e:
            fallback = self.fetch_tag_release_fallback(repo_path, source)
            if fallback:
                logger.warning(f"github-release-downloader 查询失败，已使用 tag 兜底: {str(e)}")
                return fallback, None
            return [], f"github-release-downloader 查询失败: {str(e)}"

        releases = []
        for version in versions:
            tag = getattr(version, "_origin_tag_name", str(version))
            try:
                downloader_assets = downloader_get_assets(downloader_repo, tag)
            except Exception as e:
                logger.warning(f"github-release-downloader 获取资源失败 {tag}: {str(e)}")
                downloader_assets = []

            assets = [
                {
                    "name": asset.name,
                    "size": asset.size,
                    "url": asset.url,
                    "download_backend": "github-release-downloader",
                }
                for asset in downloader_assets
            ]
            releases.append(
                {
                    "tag": tag,
                    "name": tag,
                    "published_at": "",
                    "body": "此 Release 信息由 github-release-downloader 获取。",
                    "html_url": f"https://github.com/{source.owner}/{source.repo}/releases/tag/{tag}",
                    "source": source,
                    "assets": assets,
                }
            )

        return releases, None

    def fetch_tag_release_fallback(self, repo: str, source: ReleaseSource) -> list[dict]:
        out, _, code = self.run_git(repo, ["ls-remote", "--tags", "--refs", "origin"], timeout=30)
        if code != 0 or not out.strip():
            return []

        tags = []
        for line in out.splitlines():
            if "refs/tags/" not in line:
                continue
            tag = line.rsplit("refs/tags/", 1)[-1].strip()
            if tag:
                tags.append(tag)

        return [build_tag_release_fallback(source, tag) for tag in tags[-30:][::-1]]

    def download_release_asset(self, repo: str, asset: dict, target_dir: str):
        self.executor.submit(self._download_release_asset_worker, repo, asset, target_dir)

    def _download_release_asset_worker(self, repo: str, asset: dict, target_dir: str):
        asset_name = sanitize_download_filename(str(asset.get("name") or "release-asset"))
        target_dir_abs = os.path.abspath(target_dir)
        if not os.path.isdir(target_dir_abs):
            self.notify_signal.emit("error", "失败", "下载目录不存在")
            return

        target_path = os.path.abspath(os.path.join(target_dir_abs, asset_name))
        if not target_path.startswith(target_dir_abs.rstrip("\\/") + os.sep):
            self.notify_signal.emit("error", "失败", "下载文件名不安全")
            return

        stem, suffix = os.path.splitext(asset_name)
        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.abspath(os.path.join(target_dir_abs, f"{stem}_{counter}{suffix}"))
            counter += 1

        try:
            logger.info(f"[Release 下载] {repo} -> {target_path}")
            if asset.get("download_backend") == "github-release-downloader":
                remote_url, _, _ = self.run_git(repo, ["remote", "get-url", "origin"], timeout=15)
                source = parse_release_source(remote_url)
                if source and source.host == "github.com":
                    AuthSession.init(DownloaderGitHubRepo(source.owner, source.repo, github_token()))

                downloader_asset = DownloaderReleaseAsset(
                    os.path.basename(target_path),
                    asset["url"],
                    int(asset.get("size") or 0),
                )
                downloader_download_asset(downloader_asset, Path(target_dir_abs), callback=lambda _, __: None)
                logger.success(f"Release 资源下载完成: {target_path}")
                self.notify_signal.emit("success", "下载完成", target_path)
                return

            request = urllib.request.Request(
                asset["url"],
                headers=build_release_request_headers(binary=True),
            )
            with urllib.request.urlopen(request, timeout=60) as response, open(target_path, "wb") as f:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)

            logger.success(f"Release 资源下载完成: {target_path}")
            self.notify_signal.emit("success", "下载完成", target_path)
        except Exception as e:
            logger.error(f"Release 资源下载失败: {str(e)}")
            self.notify_signal.emit("error", "下载失败", str(e)[:200])

    def run_command(self, cmd: list[str], cwd: str | None = None, timeout=60, env: dict | None = None):
        if self._is_closing:
            return "", "Application is closing", -1

        try:
            process_env = os.environ.copy()
            process_env.setdefault("PYTHONIOENCODING", "utf-8")
            if env:
                process_env.update(env)
            popen_kwargs = build_hidden_subprocess_kwargs()

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
            self._register_process(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process(proc)
                logger.error(f"命令执行超时: {' '.join(cmd)}")
                return "", "Command timed out", -1
            finally:
                self._unregister_process(proc)

            return (stdout or "").strip(), (stderr or "").strip(), proc.returncode

        except Exception as e:
            logger.error(f"命令执行失败: {str(e)}")
            return "", str(e), -1

    def _register_process(self, proc: subprocess.Popen):
        with self._process_lock:
            self._active_processes.add(proc)

    def _unregister_process(self, proc: subprocess.Popen):
        with self._process_lock:
            self._active_processes.discard(proc)

    def _terminate_process(self, proc: subprocess.Popen):
        if proc.poll() is not None:
            return

        try:
            if sys.platform == "win32":
                run_hidden(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
        except Exception as e:
            logger.debug(f"结束进程失败 PID={proc.pid}: {str(e)}")

    def _terminate_active_processes(self):
        with self._process_lock:
            processes = list(self._active_processes)

        for proc in processes:
            self._terminate_process(proc)

    # ====================== 界面方法 ======================
    def select_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.base_dir)
        if path:
            self.base_dir = path
            self.dir_label.setText(f"目录: {self.base_dir}")
            self.save_base_dir()
            logger.info(f"切换目录 → {path}")

    def apply_proxy(self):
        proxy = self.proxy_entry.text().strip()
        if proxy:
            run_hidden(["git", "config", "--global", "http.proxy", proxy])
            logger.success(f"代理已设置: {proxy}")

    def clear_proxy(self):
        run_hidden(["git", "config", "--global", "--unset", "http.proxy"])
        logger.success("代理已清除")

    def validate_github_token(self):
        token = self.github_token_entry.text().strip()
        if not token:
            InfoBar.warning("提示", "请先输入 GitHub Token", parent=self)
            return

        self.executor.submit(self._validate_github_token_worker, token)

    def _validate_github_token_worker(self, token: str):
        try:
            request = urllib.request.Request(
                "https://api.github.com/rate_limit",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "GitManager/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))

            limit = payload.get("resources", {}).get("core", {}).get("limit")
            remaining = payload.get("resources", {}).get("core", {}).get("remaining")

            config = load_config()
            config["github_token"] = token
            save_config(config)
            AuthSession.header = {"Authorization": f"Bearer {token}"}

            logger.success("GitHub Token 验证成功并已保存")
            rate_text = f"额度: {remaining}/{limit}" if limit is not None and remaining is not None else "验证成功"
            self.notify_signal.emit("success", "令牌有效", f"GitHub Token 已保存，{rate_text}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:160]
            logger.error(f"GitHub Token 验证失败: HTTP {e.code} {detail}")
            self.notify_signal.emit("error", "令牌无效", f"HTTP {e.code}: {detail}")
        except Exception as e:
            logger.error(f"GitHub Token 验证失败: {str(e)}")
            self.notify_signal.emit("error", "验证失败", str(e)[:200])

    def clear_github_token(self):
        config = load_config()
        config.pop("github_token", None)
        save_config(config)
        AuthSession.header = {}
        self.github_token_entry.clear()
        logger.success("GitHub Token 已清除")
        InfoBar.success("成功", "GitHub Token 已从配置文件清除", parent=self)

    def scan_repos(self):
        self.table.setRowCount(0)
        self.repos.clear()
        logger.info(f"开始扫描目录: {self.base_dir}")

        repo_candidates = scan_git_repos(self.base_dir)
        for candidate in repo_candidates:
            self.repos.append(candidate.path)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c in range(6):
                self.table.setItem(row, c, QTableWidgetItem("..."))
            self._add_update_button(row)

        for i, repo in enumerate(self.repos):
            self.executor.submit(self.load_repo_info, i, repo)

    def show_clone_dialog(self):
        dialog = CloneRepoDialog(self)
        if not dialog.exec():
            return

        repo_url = dialog.repo_url()
        if not repo_url:
            InfoBar.warning("提示", "请输入仓库链接", parent=self)
            return

        self.executor.submit(self.clone_repo, repo_url)

    def get_selected_repo(self):
        row = self.table.currentRow()
        if row < 0:
            return None, None

        item = self.table.item(row, 0)
        if not item:
            return row, None

        repo = item.data(Qt.UserRole) or item.text().strip()
        if not repo or repo == "...":
            return row, None

        return row, repo

    def delete_selected_repo(self):
        row, repo = self.get_selected_repo()
        if row is None or not repo:
            InfoBar.warning("提示", "请先选中一个仓库", parent=self)
            return

        repo_name = os.path.basename(repo.rstrip("\\/"))
        expected_target = os.path.abspath(os.path.join(os.path.abspath(self.base_dir), repo_name))
        selected_target = os.path.abspath(repo)
        if selected_target != expected_target:
            InfoBar.error("失败", "只允许删除当前基础目录下的直属仓库目录", parent=self)
            logger.error(f"拒绝删除非直属仓库目录: selected={selected_target}, expected={expected_target}")
            return

        box = MessageBox("确认删除仓库", f"确定删除本地仓库？\n\n{repo}", self)
        box.yesButton.setText("删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        try:
            result = safe_remove_repo_dir(self.base_dir, repo_name, onexc=self._handle_remove_readonly)
            if result["success"]:
                logger.success(f"已删除仓库: {selected_target}")
                InfoBar.success("成功", "仓库已删除", parent=self)
                self.scan_repos()
            else:
                logger.error(f"删除仓库失败: {result['error']}")
                InfoBar.error("失败", (result["error"] or "删除失败")[:150], parent=self)
        except Exception as e:
            logger.error(f"删除仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    @staticmethod
    def _handle_remove_readonly(func, path, exc_info):
        exc = exc_info[1]
        if isinstance(exc, PermissionError):
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        raise exc

    def clone_repo(self, repo_input: str | GitRepoInfo) -> dict:
        result = self.clone_git_repo(repo_input, self.base_dir)
        repo_name = derive_repo_name(repo_input)
        target_path = os.path.abspath(os.path.join(self.base_dir, repo_name)) if repo_name else ""

        if result["success"]:
            success_message = f"已克隆到: {target_path}" if target_path else "仓库克隆成功"
            logger.success(f"仓库克隆成功: {target_path or repo_name}")
            self.notify_signal.emit("success", "成功", success_message)
            self.refresh_repos_signal.emit()
        else:
            error_message = result["error"] or "克隆失败"
            logger.error(f"仓库克隆失败: {error_message}")
            self.notify_signal.emit("error", "失败", error_message[:200])

        return result

    def clone_git_repo(self, repo_input: str | GitRepoInfo, base_dir: str) -> dict:
        repo_name = derive_repo_name(repo_input)
        base_abs = os.path.abspath(base_dir)
        target_path = os.path.abspath(os.path.join(base_abs, repo_name)) if repo_name else ""

        if not repo_name:
            return {"success": False, "error": "无法从 URL 解析仓库名称"}

        if not os.path.exists(base_abs):
            return {"success": False, "error": "base_dir 不存在"}

        base_prefix = base_abs.rstrip("\\/") + os.sep
        if not target_path.startswith(base_prefix):
            return {"success": False, "error": "clone 目标路径不安全"}

        if os.path.exists(target_path):
            return {"success": False, "error": f"目录已存在: {repo_name}"}

        candidates = build_clone_candidates(repo_input)
        if not candidates:
            return {"success": False, "error": "仓库地址不能为空"}

        errors = []
        for candidate in candidates:
            logger.info(f"[克隆] 尝试源: {candidate}")
            out, err, code = self.run_command(
                ["git", "clone", candidate, target_path],
                cwd=base_abs,
                timeout=600,
            )

            if code == 0:
                return {"success": True, "error": None}

            errors.append(f"{candidate} -> {err or out or '克隆失败'}")
            if os.path.exists(target_path):
                try:
                    shutil.rmtree(target_path, onexc=self._handle_remove_readonly)
                except Exception as cleanup_error:
                    errors.append(f"清理失败: {str(cleanup_error)}")
                    break

        return {"success": False, "error": " | ".join(errors)[:500] or "克隆失败"}

    def _add_update_button(self, row: int):
        """创建更新按钮（只显示图标）"""
        btn = ToolButton(FIF.UPDATE)
        btn.setFixedWidth(85)
        btn.setToolTip("更新仓库")
        btn.clicked.connect(lambda _, r=row: self.update_single_repo(r))
        self.table.takeItem(row, 6)
        self.table.setCellWidget(row, 6, btn)
        return btn

    def update_single_repo(self, row: int):
        item = self.table.item(row, 0)
        repo = item.data(Qt.UserRole) if item else ""
        if repo:
            self.executor.submit(self.pull_repo, repo, row)

    def show_release_dialog(self, row: int):
        item = self.table.item(row, 0)
        repo = item.data(Qt.UserRole) if item else ""
        if repo and repo != "...":
            ReleaseDialog(repo, self).exec()

    def on_item_clicked(self, item):
        row = item.row()
        col = item.column()
        repo_item = self.table.item(row, 0)
        repo = repo_item.data(Qt.UserRole) if repo_item else ""

        if col == 1:  # 当前分支 → 分支管理
            BranchDialog(repo, self).exec()
        elif col == 2:  # 当前版本 → 历史
            HistoryDialog(repo, self).exec()
        elif col == 3:  # 最新版本 → diff
            self.show_diff(repo)
        elif col == 5:  # 发布版本号 → 发布版本列表
            self.show_release_dialog(row)

    def show_diff(self, repo: str):
        def run(args):
            return self.run_git(repo, args)

        def add_limited_section(title: str, text: str):
            if not text.strip():
                return
            limited_text, truncated = limit_text_bytes(text)
            sections[title + ("（已截断）" if truncated else "")] = limited_text

        sections = {}

        # 工作区
        out, _, _ = run(["diff"])
        add_limited_section("📂 工作区变更", out)

        # 暂存区
        out, _, _ = run(["diff", "--cached"])
        add_limited_section("📌 已暂存", out)

        # upstream 检查
        _, _, code = run(["rev-parse", "--symbolic-full-name", "@{u}"])
        has_upstream = code == 0
        if code == 0:
            out, _, _ = run(["diff", "HEAD", "@{u}"])
            add_limited_section("🌐 本地 vs 远程", out)

            out, _, _ = run(["log", "--oneline", "@{u}..HEAD"])
            if out.strip():
                sections["⬆ 本地领先"] = out

            out, _, _ = run(["log", "--oneline", "HEAD..@{u}"])
            if out.strip():
                sections["⬇ 远程领先"] = out
        else:
            sections["⚠ 信息"] = "未设置 upstream"

        if not sections:
            QMessageBox.information(self, "Diff", "无差异")
            return

        dlg = DiffDialog(repo, sections, self)
        dlg.exec()

    # ====================== 数据加载与按钮状态控制 ======================
    def load_repo_info(self, row: int, repo: str):
        try:
            local, _, _ = self.run_git(repo, ["rev-parse", "--short", "HEAD"])
            branch, _, _ = self.run_git(repo, ["branch", "--show-current"])
            if not branch:
                branch = "游离 HEAD"

            self.run_git(repo, ["fetch", "--quiet"])

            ahead, _, _ = self.run_git(repo, ["rev-list", "--count", "HEAD", "^@{u}"])
            behind, _, _ = self.run_git(repo, ["rev-list", "--count", "@{u}", "^HEAD"])

            ahead = int(ahead) if ahead.isdigit() else 0
            behind = int(behind) if behind.isdigit() else 0

            remote, _, rc = self.run_git(repo, ["rev-parse", "--short", "@{u}"])
            if rc != 0:
                status = "错误"
                ahead_behind = "N/A"
            elif ahead == 0 and behind == 0:
                status = "✓ 已同步"
                ahead_behind = "✓"
            else:
                status = "可更新"
                ahead_behind = f"↑{ahead} ↓{behind}"

            releases, release_error = self.fetch_releases(repo)
            if releases:
                release_text = format_release_summary(releases[0])
            elif release_error:
                release_text = "查询失败"
            else:
                release_text = "无"

            self.update_row_signal.emit(
                row, repo, branch, local or "N/A", remote or "N/A", status, ahead_behind, release_text
            )
        except Exception as e:
            logger.error(f"加载失败 {repo}: {str(e)}")
            self.update_row_signal.emit(row, repo, "N/A", "错误", "N/A", "错误", "N/A", "N/A")

    def update_table_row(
        self, row: int, repo: str, branch: str, local: str, remote: str, status: str, ahead_behind: str, release: str
    ):
        if row >= self.table.rowCount():
            return

        repo_item = QTableWidgetItem(os.path.basename(repo.rstrip("\\/")) or repo)
        repo_item.setData(Qt.UserRole, repo)
        self.table.setItem(row, 0, repo_item)

        for col, text in enumerate(
            [
                branch,
                local,
                remote,
                f"{status}  {ahead_behind}" if ahead_behind not in ("N/A", "✓") else status,
                release,
            ]
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col + 1, item)

        # 状态颜色
        status_item = self.table.item(row, 4)
        if status == "可更新":
            status_item.setForeground(QColor("#ff9800"))
        elif status == "✓ 已同步":
            status_item.setForeground(QColor("#00c853"))

        release_item = self.table.item(row, 5)
        if release_item and release not in ("无", "N/A", "查询失败"):
            release_item.setForeground(QColor("#2563eb"))
            font = release_item.font()
            font.setUnderline(True)
            release_item.setFont(font)

        # ====================== 关键修改：按钮状态控制 ======================
        btn = self.table.cellWidget(row, 6)
        if btn:
            is_updatable = status == "可更新"
            btn.setEnabled(is_updatable)
            if not is_updatable:
                btn.setStyleSheet("opacity: 0.5;")
            else:
                btn.setStyleSheet("")

    # ====================== 更新逻辑 ======================
    def pull_repo(self, repo: str, row: int):
        logger.info(f"[更新] {repo}")

        if os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")):
            logger.warning("存在未完成 merge，跳过")
            self.load_repo_info(row, repo)
            return

        clean, _, _ = self.run_git(repo, ["status", "--porcelain"])
        if clean.strip():
            logger.warning("工作区有未提交更改，跳过")
            self.load_repo_info(row, repo)
            return

        out, err, code = self.run_git(repo, ["-c", "core.editor=true", "pull", "--ff-only"])

        if code == 0:
            logger.success(f"{repo} 更新成功")
        else:
            logger.error(f"{repo} 更新失败\n{err}")

        # 刷新行（会重新设置按钮状态）
        self.load_repo_info(row, repo)

    def switch_to_commit(self, repo: str, commit: str, dialog=None):
        logger.warning(f"硬重置 {repo} → {commit}")
        _, err, code = self.run_git(repo, ["reset", "--hard", commit])

        if code == 0:
            logger.success("重置成功")
            InfoBar.success("成功", f"已切换到 {commit[:12]}", parent=self)
        else:
            logger.error(f"重置失败: {err}")
            InfoBar.error("失败", err[:150], parent=self)

        try:
            row = self.repos.index(repo)
            self.load_repo_info(row, repo)
        except ValueError:
            pass

        if dialog:
            dialog.close()

    def switch_to_branch(self, repo: str, branch_info: dict, dialog=None):
        branch_name = branch_info.get("name", "")
        is_remote = branch_info.get("is_remote", False)
        if not branch_name:
            InfoBar.warning("提示", "分支名称为空", parent=self)
            return

        current_branch, _, _ = self.run_git(repo, ["branch", "--show-current"])
        if current_branch == branch_name:
            InfoBar.warning("提示", "当前已经在该分支", parent=self)
            if dialog:
                dialog.close()
            return

        logger.info(f"切换分支 {repo} → {branch_name}")

        if is_remote:
            local_branch = branch_name.split("/", 1)[1] if "/" in branch_name else branch_name
            local_match, _, _ = self.run_git(repo, ["branch", "--list", local_branch])
            if local_match.strip():
                switch_args = ["switch", local_branch]
            else:
                switch_args = ["switch", "--track", branch_name]
        else:
            switch_args = ["switch", branch_name]

        _, err, code = self.run_git(repo, switch_args)

        if code == 0:
            logger.success(f"分支切换成功: {branch_name}")
            InfoBar.success("成功", f"已切换到 {branch_name}", parent=self)
        else:
            logger.error(f"分支切换失败: {err}")
            InfoBar.error("失败", (err or "分支切换失败")[:150], parent=self)

        try:
            row = self.repos.index(repo)
            self.load_repo_info(row, repo)
        except ValueError:
            pass

        if dialog and code == 0:
            dialog.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitManager()
    window.show()
    sys.exit(app.exec())
