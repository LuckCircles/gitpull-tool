import os
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Action
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    HorizontalSeparator,
    InfoBar,
    MessageBoxBase,
    MSFluentWindow,
    NavigationItemPosition,
    PrimaryPushButton,
    PushButton,
    PushSettingCard,
    RoundMenu,
    SearchLineEdit,
    SettingCardGroup,
    StrongBodyLabel,
    SwitchSettingCard,
    TableWidget,
    TextEdit,
    Theme,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
    setTheme,
)

from app.config import load_config, save_config, save_repo_cache
from core.git_service import GitService
from core.repo_service import RepoService
from core.scan_service import ScanService
from core.update_service import UpdateService
from res_rc import qInitResources
from ui.dialogs.branch_dialog import BranchDialog
from ui.dialogs.clone_dialog import CloneRepoDialog
from ui.dialogs.delete_dialog import DeleteRepoDialog
from ui.dialogs.history_dialog import HistoryDialog
from ui.dialogs.rename_dialog import RenameRepoDialog
from workers.proxy_verify_worker import ProxyVerifyWorker


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


# ====================== 主窗口 ======================
class GitManager(MSFluentWindow):
    update_row_signal = Signal(
        int, str, str, str, str, str, str, str
    )  # row, repo, branch, local, remote_commit, status, ahead_behind, remote_url
    notify_signal = Signal(str, str, str)
    scan_summary_signal = Signal(
        int, int, int
    )  # need_update_count, ignored_count, total_count
    update_complete_signal = Signal(str, bool, str)  # repo_name, success, message

    def __init__(self):
        super().__init__()
        qInitResources()
        setTheme(Theme.LIGHT)
        self.git_service = GitService()
        self.update_service = UpdateService(self.git_service)
        self.git_service.configure_global_quotepath()

        self.setWindowTitle("Git 多仓库管理器 - 高级版")
        self.setWindowIcon(QIcon(":/icon.ico"))
        self.resize(1280, 800)

        self._config_cache = self._load_cached_config()
        self.base_dir = self.load_base_dir()
        self.repos: list[str] = []
        self._repo_cache: dict[str, dict] = {}  # path -> cached info
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._scan_lock = threading.Lock()
        self._scan_generation = 0
        self._scan_expected = 0
        self._scan_done = 0
        self._scan_need_update = 0
        self._scan_ignored = 0
        self._proxy_verify_thread = None  # 代理验证线程

        self.init_ui()
        self.apply_proxy(self.load_proxy())

        self.qt_handler = QtLogHandler(self.log_text)
        logger.add(
            self.qt_handler.write,
            level="DEBUG",
            format="{time:HH:mm:ss} | <level>{level:8}</level> | {message}",
        )
        logger.success("Git 多仓库管理器启动成功")

    def _load_cached_config(self) -> dict:
        return load_config()

    def _save_cached_config(self):
        save_config(self._config_cache)

    def closeEvent(self, event):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.git_service.shutdown()
        # 保存仓库缓存
        save_repo_cache(list(self._repo_cache.values()))
        logger.remove()
        super().closeEvent(event)

    def center_window(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        screen_geometry = screen.availableGeometry()
        x = screen_geometry.x() + (screen_geometry.width() - self.width()) // 2
        y = screen_geometry.y() + (screen_geometry.height() - self.height()) // 2
        self.move(x, y)

    def load_base_dir(self) -> str:
        default_dir = os.getcwd()
        try:
            config = self._config_cache
            saved_dir = str(config.get("base_dir") or "").strip()
            if saved_dir and os.path.isdir(saved_dir):
                return saved_dir
        except Exception as e:
            logger.warning(f"加载配置失败: {str(e)}")

        return default_dir

    def load_proxy(self) -> str:
        config = self._config_cache
        return str(config.get("proxy") or "http://127.0.0.1:7897").strip()

    def load_token(self) -> str:
        config = self._config_cache
        return str(config.get("token") or "").strip()

    # ====================== 忽略更新管理 ======================
    def _get_ignore_list(self) -> list[str]:
        """获取忽略更新仓库路径列表（确保为 list 且元素为绝对路径）。"""
        raw = self._config_cache.get("ignore_update_repos")
        if not isinstance(raw, list):
            raw = []
        return [os.path.abspath(p) for p in raw if isinstance(p, str) and p.strip()]

    def is_repo_ignored(self, repo_path: str) -> bool:
        """判断指定仓库是否在忽略更新列表中。"""
        if not repo_path:
            return False
        return os.path.abspath(repo_path) in self._get_ignore_list()

    def ignore_repo_update(self, repo_path: str):
        """将仓库加入忽略更新列表并保存配置。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path not in ignore_list:
            ignore_list.append(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list
            self._save_cached_config()
            logger.info(f"[忽略更新] 已添加: {abs_path}")

    def restore_repo_update(self, repo_path: str):
        """将仓库从忽略更新列表移除并保存配置。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path in ignore_list:
            ignore_list.remove(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list
            self._save_cached_config()
            logger.info(f"[恢复更新] 已移除: {abs_path}")

    def _remove_ignored_record(self, repo_path: str):
        """删除仓库时同步清除忽略记录（静默操作，不保存配置）。"""
        abs_path = os.path.abspath(repo_path)
        ignore_list = self._get_ignore_list()
        if abs_path in ignore_list:
            ignore_list.remove(abs_path)
            self._config_cache["ignore_update_repos"] = ignore_list

    def save_base_dir(self):
        try:
            self._config_cache["base_dir"] = self.base_dir
            self._save_cached_config()
        except Exception as e:
            logger.warning(f"保存配置失败: {str(e)}")

    def save_settings(self, base_dir: str, proxy: str, token: str):
        self._config_cache["base_dir"] = base_dir
        self._config_cache["proxy"] = proxy
        self._config_cache["token"] = token
        self._save_cached_config()

    def init_ui(self):
        self.repo_page = QWidget()
        self.repo_page.setObjectName("repo_page")
        repo_layout = QVBoxLayout(self.repo_page)
        repo_layout.setContentsMargins(16, 12, 16, 12)
        repo_layout.setSpacing(12)

        top = QHBoxLayout()
        self.dir_label = StrongBodyLabel(f"目录: {self.base_dir}")
        top.addWidget(self.dir_label)
        top.addStretch()

        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText("搜索仓库...")
        self.search_edit.setFixedWidth(200)
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_repos)
        top.addWidget(self.search_edit)

        self.scan_btn = PushButton(FIF.SYNC, "扫描仓库")
        self.scan_btn.clicked.connect(self.scan_repos)
        self.add_repo_btn = PushButton(FIF.ADD, "添加仓库")
        self.add_repo_btn.clicked.connect(self.show_clone_dialog)
        self.remove_repo_btn = PushButton(FIF.DELETE, "删除仓库")
        self.remove_repo_btn.clicked.connect(self.delete_selected_repo)
        self.bulk_update_btn = PrimaryPushButton(FIF.UPDATE, "一键更新")
        self.bulk_update_btn.clicked.connect(self.update_checked_repos)

        top.addWidget(self.scan_btn)
        top.addWidget(self.add_repo_btn)
        top.addWidget(self.remove_repo_btn)
        top.addWidget(self.bulk_update_btn)
        repo_layout.addLayout(top)

        repo_layout.addWidget(HorizontalSeparator())

        self.table = TableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["", "仓库名称", "当前分支", "当前版本", "最新版本", "状态 / 同步", "操作"]
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)

        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 170)
        self.table.setColumnWidth(6, 90)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        self.table.itemClicked.connect(self.on_item_clicked)
        repo_layout.addWidget(self.table, 1)

        self.log_page = QWidget()
        self.log_page.setObjectName("log_page")
        log_page_layout = QVBoxLayout(self.log_page)
        log_page_layout.setContentsMargins(16, 12, 16, 12)
        log_page_layout.setSpacing(12)
        log_page_layout.addWidget(StrongBodyLabel("运行日志"))
        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        log_page_layout.addWidget(self.log_text, 1)

        self.settings_page = QWidget()
        self.settings_page.setObjectName("settings_page")
        settings_layout = QVBoxLayout(self.settings_page)
        settings_layout.setContentsMargins(24, 20, 24, 20)
        settings_layout.setSpacing(20)

        # ========== 目录设置 ==========
        self.dir_group = SettingCardGroup("存储目录", self.settings_page)
        self.dir_card = PushSettingCard(
            "选择目录",
            FIF.FOLDER,
            "仓库存放目录",
            "所有 Git 仓库将被存放在此目录下",
            self.dir_group,
        )
        self.dir_card.setContent(self.base_dir)
        self.dir_card.clicked.connect(self.select_settings_dir)
        self.dir_group.addSettingCard(self.dir_card)
        settings_layout.addWidget(self.dir_group)

        # ========== 网络设置 ==========
        self.network_group = SettingCardGroup("网络设置", self.settings_page)

        self.proxy_card = PushSettingCard(
            "点击编辑",
            FIF.GLOBE,
            "代理地址",
            "用于加速 Git 克隆和拉取，如 http://127.0.0.1:7897",
            self.network_group,
        )
        self.proxy_card.setContent(self.load_proxy() or "未设置")
        self.proxy_card.clicked.connect(self._edit_proxy)
        self.network_group.addSettingCard(self.proxy_card)

        # 验证代理卡片
        self.verify_proxy_card = PushSettingCard(
            "验证连接",
            FIF.PLAY,
            "验证代理",
            "验证代理配置和 GitHub 连接性",
            self.network_group,
        )
        self.verify_proxy_card.clicked.connect(self.verify_proxy_settings)
        self.network_group.addSettingCard(self.verify_proxy_card)

        self.token_card = PushSettingCard(
            "点击编辑",
            FIF.HEART,
            "访问令牌",
            "用于私有仓库认证，支持 GitHub / Gitee Token",
            self.network_group,
        )
        token = self.load_token()
        self.token_card.setContent(self._mask_token(token))
        self.token_card.clicked.connect(self._edit_token)
        self.network_group.addSettingCard(self.token_card)

        self.proxy_switch = SwitchSettingCard(
            FIF.POWER_BUTTON,
            "启用代理",
            content="关闭后 Git 操作将不使用代理",
            parent=self.network_group,
        )
        self.proxy_switch.setChecked(True)
        self.proxy_switch.checkedChanged.connect(self._on_proxy_toggle)
        self.network_group.addSettingCard(self.proxy_switch)

        settings_layout.addWidget(self.network_group)

        # ========== 关于 ==========
        self.about_group = SettingCardGroup("关于", self.settings_page)
        self.about_card = PushSettingCard(
            "查看",
            FIF.INFO,
            "Git 多仓库管理器",
            "高级版 · 支持批量管理、分支切换、版本回退",
            self.about_group,
        )
        self.about_card.clicked.connect(
            lambda: InfoBar.info(
                "关于",
                "Git 多仓库管理器 v2.0\n基于 PySide6 + FluentWidgets 构建",
                parent=self,
            )
        )
        self.about_group.addSettingCard(self.about_card)
        settings_layout.addWidget(self.about_group)

        # ========== 底部保存按钮 ==========
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 8, 0, 0)

        self.save_btn = PrimaryPushButton(FIF.SAVE, "保存设置")
        self.save_btn.setMinimumWidth(160)
        self.save_btn.clicked.connect(self.apply_settings_from_page)

        self.reset_btn = PushButton(FIF.CANCEL, "重置")
        self.reset_btn.setMinimumWidth(100)
        self.reset_btn.clicked.connect(self._reset_settings)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.reset_btn)
        bottom_layout.addWidget(self.save_btn)
        settings_layout.addWidget(bottom_widget)

        settings_layout.addStretch(1)

        self.addSubInterface(self.repo_page, FIF.HOME, "仓库")
        self.addSubInterface(self.log_page, FIF.DOCUMENT, "日志")
        self.addSubInterface(
            self.settings_page,
            FIF.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

        self.update_row_signal.connect(self.update_table_row)
        self.notify_signal.connect(self.show_notification)
        self.scan_summary_signal.connect(self.show_scan_summary)
        self.update_complete_signal.connect(self.show_update_complete)

        # 启动后保持空闲，仓库列表只在用户点击“扫描仓库”后刷新。

    def _load_repo_cache_startup(self):
        """启动时不加载、不扫描、不刷新仓库列表。"""
        return

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
        return self.git_service.run_git(path, args, timeout=timeout)

    def run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout=60,
        env: dict | None = None,
    ):
        return self.git_service.run_command(cmd, cwd=cwd, timeout=timeout, env=env)

    # ====================== 界面方法 ======================
    def select_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.base_dir)
        if path:
            self.base_dir = path
            self.dir_label.setText(f"目录: {self.base_dir}")
            self.save_base_dir()
            logger.info(f"切换目录 → {path}")

    def apply_proxy(self, proxy: str | None = None):
        if proxy is None:
            proxy = self.load_proxy()
        proxy = proxy.strip()
        if proxy:
            self.git_service.set_global_proxy(proxy)
            logger.success(f"代理已设置: {proxy}")
        else:
            self.clear_proxy()

    def clear_proxy(self):
        self.git_service.clear_global_proxy()
        logger.success("代理已清除")

    def select_settings_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.base_dir)
        if path:
            self.dir_card.setContent(path)

    def apply_settings_from_page(self):
        base_dir = self.dir_card.contentLabel.text().strip() or self.base_dir
        proxy = self.proxy_card.contentLabel.text().strip()
        token = self.token_card.contentLabel.text().strip()
        proxy_enabled = self.proxy_switch.isChecked()

        if proxy == "未设置":
            proxy = ""

        if token == "未设置":
            token = ""

        self.base_dir = base_dir
        self.dir_label.setText(f"目录: {self.base_dir}")
        self.save_settings(base_dir, proxy, token)
        if proxy_enabled and proxy:
            self.apply_proxy(proxy)
        else:
            self.clear_proxy()
        logger.success("设置已保存")
        InfoBar.success("成功", "设置已保存并应用", parent=self)

    def _edit_proxy(self):
        dialog = MessageBoxBase(self)
        dialog.titleLabel = SubtitleLabel("编辑代理地址")
        dialog.cancelButton.setText("取消")
        dialog.yesButton.setText("确定")

        editor = LineEdit()
        editor.setText(self.load_proxy())
        editor.setClearButtonEnabled(True)
        editor.setPlaceholderText("例如: http://127.0.0.1:7897")
        dialog.viewLayout.addWidget(dialog.titleLabel)
        dialog.viewLayout.addWidget(editor)
        dialog.widget.setMinimumWidth(420)

        if dialog.exec():
            new_proxy = editor.text().strip()
            self.proxy_card.setContent(new_proxy or "未设置")

    def _edit_token(self):
        dialog = MessageBoxBase(self)
        dialog.titleLabel = SubtitleLabel("编辑访问令牌")
        dialog.cancelButton.setText("取消")
        dialog.yesButton.setText("确定")

        editor = LineEdit()
        editor.setText(self.load_token())
        editor.setClearButtonEnabled(True)
        editor.setPlaceholderText("支持 GitHub / Gitee Token")
        dialog.viewLayout.addWidget(dialog.titleLabel)
        dialog.viewLayout.addWidget(editor)
        dialog.widget.setMinimumWidth(420)

        if dialog.exec():
            new_token = editor.text().strip()
            self.token_card.setContent(self._mask_token(new_token))

    def _reset_settings(self):
        self.proxy_card.setContent(self.load_proxy() or "未设置")
        token = self.load_token()
        self.token_card.setContent(self._mask_token(token))
        self.dir_card.setContent(self.base_dir)
        self.proxy_switch.setChecked(True)
        InfoBar.info("提示", "已重置为当前保存的设置", parent=self)

    def _on_proxy_toggle(self, checked: bool):
        proxy = self.proxy_card.contentLabel.text().strip()
        if proxy == "未设置":
            proxy = ""
        if checked and proxy:
            self.apply_proxy(proxy)
        else:
            self.clear_proxy()

    @staticmethod
    def _mask_token(token: str) -> str:
        if not token:
            return "未设置"
        if len(token) <= 8:
            return (
                token[:2] + "*" * (len(token) - 4) + token[-2:]
                if len(token) >= 4
                else "****"
            )
        return token[:4] + "*" * (len(token) - 8) + token[-4:]

    def _filter_repos(self, keyword: str):
        keyword = keyword.strip().lower()
        for row in range(self.table.rowCount()):
            repo_item = self.table.item(row, 1)
            if not repo_item:
                self.table.setRowHidden(row, bool(keyword))
                continue
            repo = repo_item.data(Qt.UserRole) or ""
            name = os.path.basename(repo.rstrip("\\/") or repo)
            self.table.setRowHidden(row, keyword not in name.lower())

    def scan_repos(self):
        self.table.setRowCount(0)
        self.repos.clear()
        self._repo_cache.clear()
        logger.info(f"开始扫描目录: {self.base_dir}")

        repo_candidates = ScanService.scan_git_repos(self.base_dir)
        with self._scan_lock:
            self._scan_generation += 1
            generation = self._scan_generation
            self._scan_expected = len(repo_candidates)
            self._scan_done = 0
            self._scan_need_update = 0
            self._scan_ignored = 0

        for candidate in repo_candidates:
            self.repos.append(candidate.path)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c in range(7):
                item = QTableWidgetItem("...")
                # 在第1列设置repo_path作为UserRole数据
                if c == 1:
                    item.setData(Qt.UserRole, candidate.path)
                self.table.setItem(row, c, item)
            self._add_update_button(row, candidate.path)

        if not repo_candidates:
            self.scan_summary_signal.emit(0, 0, 0)
            return

        for repo in self.repos:
            self.executor.submit(self.load_repo_info, repo, generation)

    def _mark_scan_progress(
        self, generation: int, need_update: bool, ignored: bool = False
    ):
        with self._scan_lock:
            if generation != self._scan_generation:
                return

            self._scan_done += 1
            if need_update:
                self._scan_need_update += 1
            if ignored:
                self._scan_ignored += 1

            if self._scan_done == self._scan_expected:
                self.scan_summary_signal.emit(
                    self._scan_need_update, self._scan_ignored, self._scan_expected
                )

    def show_scan_summary(
        self, need_update_count: int, ignored_count: int, total_count: int
    ):
        parts = [f"共扫描 {total_count} 个仓库"]
        if need_update_count > 0:
            parts.append(f"需要更新 {need_update_count} 个")
        if ignored_count > 0:
            parts.append(f"已忽略更新 {ignored_count} 个")
        summary = "\n".join(parts)

        if need_update_count == 0 and ignored_count == 0:
            InfoBar.success("扫描完成", summary, duration=4000, parent=self)
        elif need_update_count == 0:
            InfoBar.success("扫描完成", summary, duration=5000, parent=self)
        else:
            InfoBar.warning("扫描完成", summary, duration=5000, parent=self)

    def show_update_complete(self, repo_name: str, success: bool, message: str):
        if success:
            InfoBar.success(
                "更新成功", f"{repo_name} 已是最新版本", duration=4000, parent=self
            )
        else:
            InfoBar.error(
                "更新失败",
                f"{repo_name} 更新失败。\n{message}",
                duration=5000,
                parent=self,
            )

    def show_clone_dialog(self):
        """打开克隆仓库对话框，对话框内部管理整个克隆生命周期。"""
        dialog = CloneRepoDialog(self)
        dialog.exec()

    def get_selected_repo(self):
        row = self.table.currentRow()
        if row < 0:
            return None, None

        item = self.table.item(row, 0)
        if not item:
            return row, None

        repo_item = self.table.item(row, 1)
        repo = (repo_item.data(Qt.UserRole) if repo_item else "") or ""
        if not repo or repo == "...":
            return row, None

        return row, repo

    def delete_selected_repo(self):
        row, repo = self.get_selected_repo()
        if row is None or not repo:
            InfoBar.warning("提示", "请先选中一个仓库", parent=self)
            return

        self._context_delete_repo(repo)

    def get_row_by_repo_path(self, repo_path: str) -> int:
        """根据repo_path查找当前表格中的行号。不存在返回-1"""
        repo_path = os.path.abspath(repo_path)
        for row in range(self.table.rowCount()):
            repo_item = self.table.item(row, 1)
            if repo_item:
                item_repo = repo_item.data(Qt.UserRole)
                if item_repo and os.path.abspath(item_repo) == repo_path:
                    return row
        return -1

    def _add_update_button(self, row: int, repo_path: str):
        """创建更新按钮（只显示图标）。使用repo_path而不是row作为标识"""
        btn = ToolButton(FIF.UPDATE)
        btn.setFixedWidth(85)
        btn.installEventFilter(ToolTipFilter(btn, 0, ToolTipPosition.BOTTOM))
        btn.setToolTip("更新仓库")
        btn.clicked.connect(lambda: self.update_single_repo(repo_path))
        self.table.takeItem(row, 6)
        self.table.setCellWidget(row, 6, btn)
        return btn

    def _add_repo_row(self, repo_path: str):
        """直接添加一行仓库到表格，不重新扫描"""
        repo_path = os.path.abspath(repo_path)
        if repo_path in self.repos:
            return
        self.repos.append(repo_path)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for c in range(7):
            self.table.setItem(row, c, QTableWidgetItem("..."))
        self._add_update_button(row, repo_path)
        # 撤销搜索过滤确保新行可见
        self.search_edit.clear()
        # 加载仓库信息
        self.executor.submit(self.load_repo_info, repo_path, None)

    def update_single_repo(self, repo_path: str):
        """通过repo_path更新单个仓库。自动查找当前行号"""
        repo_path = os.path.abspath(repo_path)
        row = self.get_row_by_repo_path(repo_path)
        if row == -1:
            logger.warning(f"未找到仓库行: {repo_path}")
            return
        self.executor.submit(self.pull_repo, repo_path)

    def get_checked_repos(self) -> list[tuple[int, str]]:
        checked: list[tuple[int, str]] = []
        for row in range(self.table.rowCount()):
            check_item = self.table.item(row, 0)
            repo_item = self.table.item(row, 1)
            if not check_item or not repo_item:
                continue
            repo = repo_item.data(Qt.UserRole)
            if repo and check_item.checkState() == Qt.Checked:
                checked.append((row, repo))
        return checked

    def update_checked_repos(self):
        checked_repos = self.get_checked_repos()
        if not checked_repos:
            InfoBar.warning("提示", "请先勾选要更新的仓库", parent=self)
            return

        skipped = 0
        to_update = []
        for row, repo in checked_repos:
            if self.is_repo_ignored(repo):
                repo_name = os.path.basename(repo.rstrip("\\/"))
                logger.info(f"[跳过更新] {repo_name} 已设置忽略更新")
                skipped += 1
            else:
                to_update.append(repo)

        if skipped:
            InfoBar.warning(
                "跳过已忽略",
                f"已跳过 {skipped} 个忽略更新的仓库",
                duration=4000,
                parent=self,
            )

        if not to_update:
            if skipped:
                InfoBar.warning(
                    "提示", "选中的仓库均已忽略更新，无可执行项", parent=self
                )
            return

        for repo in to_update:
            self.executor.submit(self.pull_repo, repo)
        logger.info(f"[批量更新] 已提交 {len(to_update)} 个仓库")
        InfoBar.success(
            "已开始",
            f"正在更新 {len(to_update)} 个仓库"
            + (f"（跳过 {skipped} 个已忽略）" if skipped else ""),
            parent=self,
        )

    def on_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return

        row = item.row()
        repo_item = self.table.item(row, 1)
        repo = repo_item.data(Qt.UserRole) if repo_item else ""
        if not repo or repo == "...":
            return

        menu = RoundMenu(parent=self)

        menu.addAction(
            Action(
                FIF.FOLDER, "打开本地", triggered=lambda: self._open_local_folder(repo)
            )
        )

        cache = self._repo_cache.get(repo, {})
        remote_url = cache.get("remote_url", "")
        open_remote = Action(
            FIF.GLOBE, "打开远端", triggered=lambda: self._open_remote_url(repo)
        )
        if not remote_url:
            open_remote.setEnabled(False)
        menu.addAction(open_remote)

        menu.addSeparator()

        # 忽略更新 / 恢复更新（动态显示）
        if self.is_repo_ignored(repo):
            menu.addAction(
                Action(
                    FIF.PLAY,
                    "继续更新",
                    triggered=lambda: self._toggle_ignore_update(repo, ignore=False),
                )
            )
        else:
            menu.addAction(
                Action(
                    FIF.PAUSE,
                    "忽略更新",
                    triggered=lambda: self._toggle_ignore_update(repo, ignore=True),
                )
            )

        menu.addSeparator()

        menu.addAction(
            Action(
                FIF.EDIT,
                "重命名仓库",
                triggered=lambda: self._context_rename_repo(repo),
            )
        )
        menu.addAction(
            Action(
                FIF.DELETE,
                "删除仓库",
                triggered=lambda: self._context_delete_repo(repo),
            )
        )

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _toggle_ignore_update(self, repo_path: str, ignore: bool):
        """切换仓库的忽略更新状态。使用repo_path而不是row"""
        repo_name = os.path.basename(repo_path.rstrip("\\/"))
        if ignore:
            self.ignore_repo_update(repo_path)
            InfoBar.success(
                "忽略更新",
                f"{repo_name} 仓库已忽略更新检查",
                duration=4000,
                parent=self,
            )
        else:
            self.restore_repo_update(repo_path)
            InfoBar.success(
                "恢复更新",
                f"{repo_name} 仓库已恢复更新检查",
                duration=4000,
                parent=self,
            )
        # 立即刷新当前仓库状态（自动查找行号）
        self.executor.submit(self.load_repo_info, repo_path, None)

    def _open_local_folder(self, repo: str):
        path = os.path.abspath(repo)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        logger.info(f"打开本地目录: {path}")

    def _open_remote_url(self, repo: str):
        cache = self._repo_cache.get(repo, {})
        remote_url = cache.get("remote_url", "")
        if not remote_url:
            InfoBar.warning("提示", "未找到远端地址", parent=self)
            return

        webbrowser.open(remote_url)
        logger.info(f"打开远端地址: {remote_url}")

    def _context_delete_repo(self, repo_path: str):
        """删除仓库。支持完全删除和仅删除Git两种模式"""
        repo_path = os.path.abspath(repo_path)
        repo_name = os.path.basename(repo_path.rstrip("\\/"))
        selected_target = repo_path
        if not RepoService.is_direct_child(self.base_dir, repo_path):
            InfoBar.error("失败", "只允许删除当前基础目录下的直属仓库目录", parent=self)
            return

        # 显示删除对话框
        dlg = DeleteRepoDialog(repo_path, parent=self)
        if not dlg.exec():
            return

        try:
            if dlg.is_delete_all():
                # 完全删除整个仓库目录
                result = RepoService.remove_repo_dir(
                    self.base_dir,
                    repo_name,
                    onerror=RepoService.make_writable_and_retry,
                )
                delete_type = "仓库"
            else:
                # 仅删除.git文件夹
                result = RepoService.remove_git_metadata(
                    repo_path, onerror=RepoService.make_writable_and_retry
                )
                delete_type = ".git文件夹"

            if result["success"]:
                logger.success(f"已删除{delete_type}: {selected_target}")

                # 仅当完全删除时才移除表格行和缓存
                if dlg.is_delete_all():
                    row = self.get_row_by_repo_path(repo_path)
                    if row != -1:
                        self.table.removeRow(row)
                    try:
                        self.repos.remove(repo_path)
                    except ValueError:
                        pass
                    self._repo_cache.pop(repo_path, None)
                    self._remove_ignored_record(repo_path)
                    self._save_cached_config()

                InfoBar.success("成功", f"{delete_type}已删除", parent=self)
            else:
                logger.error(f"删除{delete_type}失败: {result['error']}")
                InfoBar.error(
                    "失败", (result["error"] or "删除失败")[:150], parent=self
                )
        except Exception as e:
            logger.error(f"删除仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    def _context_rename_repo(self, repo_path: str):
        """重命名仓库目录"""
        repo_path = os.path.abspath(repo_path)
        repo_name = os.path.basename(repo_path.rstrip("\\/"))
        if not RepoService.is_direct_child(self.base_dir, repo_path):
            InfoBar.error(
                "失败", "只允许重命名当前基础目录下的直属仓库目录", parent=self
            )
            return

        # 显示重命名对话框
        dlg = RenameRepoDialog(repo_name, parent=self)
        if not dlg.exec():
            return

        new_name = dlg.get_new_name()

        # 验证新名称
        if new_name == repo_name:
            InfoBar.info("提示", "新名称与当前名称相同", parent=self)
            return

        rename_result = RepoService.rename_repo(self.base_dir, repo_path, new_name)
        if not rename_result["success"]:
            InfoBar.error("失败", rename_result["error"][:150], parent=self)
            return

        new_path = rename_result["path"]
        try:
            logger.success(f"已重命名仓库: {repo_name} → {new_name}")

            # 更新repos列表中的路径
            try:
                idx = self.repos.index(repo_path)
                self.repos[idx] = new_path
            except ValueError:
                pass

            # 更新缓存
            if repo_path in self._repo_cache:
                self._repo_cache[new_path] = self._repo_cache.pop(repo_path)

            # 更新忽略记录
            if self.is_repo_ignored(repo_path):
                self._ignored_repos.discard(repo_path)
                self._ignored_repos.add(new_path)

            # 更新表格中的repo_path
            row = self.get_row_by_repo_path(repo_path)
            if row != -1:
                repo_item = self.table.item(row, 1)
                if repo_item:
                    repo_item.setData(Qt.UserRole, new_path)

            self._save_cached_config()
            InfoBar.success("成功", f"已重命名为 '{new_name}'", parent=self)
        except Exception as e:
            logger.error(f"重命名仓库失败: {str(e)}")
            InfoBar.error("失败", str(e)[:150], parent=self)

    def verify_proxy_settings(self):
        """验证代理设置和GitHub连接性"""
        proxy = self.load_proxy()

        # 显示验证开始
        InfoBar.info("验证中...", "正在验证代理配置和连接性，请稍候...", parent=self)

        # 创建工作线程
        self._proxy_verify_worker = ProxyVerifyWorker(proxy, timeout=10)
        self._proxy_verify_thread = QThread()
        self._proxy_verify_worker.moveToThread(self._proxy_verify_thread)

        # 连接信号
        self._proxy_verify_worker.finished.connect(self._on_proxy_verify_finished)
        self._proxy_verify_thread.started.connect(self._proxy_verify_worker.run)

        # 启动线程
        self._proxy_verify_thread.start()

    def _on_proxy_verify_finished(self, success: bool, message: str):
        """代理验证完成回调"""
        # 清理线程
        if self._proxy_verify_thread:
            self._proxy_verify_thread.quit()
            self._proxy_verify_thread.wait()
            self._proxy_verify_thread = None

        # 显示结果
        if success:
            InfoBar.success("验证成功", message, parent=self)
            logger.success(f"代理验证通过: {message}")
        else:
            InfoBar.error("验证失败", message[:150], parent=self)
            logger.error(f"代理验证失败: {message}")

    # ====================== 数据加载与按钮状态控制 ======================
    def load_repo_info(self, repo_path: str, generation: int | None = None):
        """加载仓库信息。自动查找当前行号，不需要显式传递row"""
        repo_path = os.path.abspath(repo_path)
        row = self.get_row_by_repo_path(repo_path)
        if row == -1:
            logger.warning(f"未找到仓库行: {repo_path}")
            return

        need_update = False
        ignored = False
        try:
            repo_status = self.git_service.inspect_repository(
                repo_path, ignored=self.is_repo_ignored(repo_path)
            )
            need_update = repo_status.need_update
            ignored = repo_status.ignored

            self.update_row_signal.emit(
                row,
                repo_path,
                repo_status.branch,
                repo_status.local_commit,
                repo_status.remote_commit,
                repo_status.status,
                repo_status.ahead_behind,
                repo_status.remote_url,
            )
        except Exception as e:
            logger.error(f"加载失败 {repo_path}: {str(e)}")
            self.update_row_signal.emit(
                row, repo_path, "N/A", "错误", "N/A", "错误", "N/A", ""
            )
        finally:
            if generation is not None:
                self._mark_scan_progress(generation, need_update, ignored)

    def update_table_row(
        self,
        row: int,
        repo: str,
        branch: str,
        local: str,
        remote_commit: str,
        status: str,
        ahead_behind: str,
        remote_url: str = "",
    ):
        if row >= self.table.rowCount():
            return

        old_check_item = self.table.item(row, 0)
        old_check_state = (
            old_check_item.checkState() if old_check_item else Qt.Unchecked
        )

        check_item = QTableWidgetItem("")
        is_updatable = status == "可更新"
        is_ignored = status == "⏸ 已忽略更新"
        if is_updatable:
            check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check_item.setCheckState(old_check_state)
        else:
            check_item.setFlags(Qt.NoItemFlags)
            check_item.setCheckState(Qt.Unchecked)
        check_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 0, check_item)

        repo_name = os.path.basename(repo.rstrip("\\/")) or repo
        repo_item = QTableWidgetItem(repo_name)
        repo_item.setData(Qt.UserRole, repo)
        repo_item.setToolTip(repo)
        self.table.setItem(row, 1, repo_item)

        status_text = (
            f"{status}  {ahead_behind}"
            if ahead_behind not in ("N/A", "✓", "-")
            else status
        )
        for col, text in enumerate([branch, local, remote_commit, status_text]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col + 2, item)

        # 状态颜色
        status_item = self.table.item(row, 5)
        if status == "可更新":
            status_item.setForeground(QColor("#ff9800"))
        elif status == "✓ 已同步":
            status_item.setForeground(QColor("#00c853"))
        elif is_ignored:
            status_item.setForeground(QColor("#9e9e9e"))

        # 按钮状态控制
        btn = self.table.cellWidget(row, 6)
        if btn:
            btn.setEnabled(is_updatable)
            if not is_updatable:
                btn.setStyleSheet("opacity: 0.5;")
            else:
                btn.setStyleSheet("")

        # 更新缓存
        self._repo_cache[repo] = {
            "name": repo_name,
            "path": repo,
            "remote_url": remote_url or "",
            "branch": branch,
            "local": local,
            "remote": remote_commit,
            "status": status,
            "ahead_behind": ahead_behind,
        }

    # ====================== 更新逻辑 ======================
    def pull_repo(self, repo: str):
        """拉取仓库更新。不需要row参数，通过repo_path查找行号"""
        repo = os.path.abspath(repo)
        logger.info(f"[更新] {repo}")

        result = self.update_service.update_repo(
            repo, ignored=self.is_repo_ignored(repo)
        )

        if result.attempted:
            self.load_repo_info(repo, None)

        if result.success:
            logger.success(f"{repo} 更新成功")
            self.update_complete_signal.emit(result.repo_name, True, "")
        else:
            logger.error(f"{repo} 更新失败\n{result.message}")
            self.update_complete_signal.emit(result.repo_name, False, result.message)

    def on_item_clicked(self, item):
        row = item.row()
        col = item.column()
        repo_item = self.table.item(row, 1)
        repo = repo_item.data(Qt.UserRole) if repo_item else ""

        if col == 2:  # 当前分支 → 分支管理
            BranchDialog(repo, self).exec()
        elif col == 3:  # 当前版本 → 历史
            HistoryDialog(repo, self).exec()

    def switch_to_commit(self, repo: str, commit: str, dialog=None):
        logger.warning(f"硬重置 {repo} → {commit}")
        result = self.git_service.reset_to_commit(repo, commit)

        if result.success:
            logger.success("重置成功")
            InfoBar.success("成功", f"已切换到 {commit[:12]}", parent=self)
        else:
            logger.error(f"重置失败: {result.error}")
            InfoBar.error("失败", result.error[:150], parent=self)

        try:
            self.load_repo_info(repo, None)
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

        result = self.git_service.switch_branch(
            repo, branch_name, is_remote=is_remote
        )
        if result.already_active:
            InfoBar.warning("提示", "当前已经在该分支", parent=self)
            if dialog:
                dialog.close()
            return

        logger.info(f"切换分支 {repo} → {branch_name}")

        if result.success:
            logger.success(f"分支切换成功: {branch_name}")
            InfoBar.success("成功", f"已切换到 {branch_name}", parent=self)
        else:
            logger.error(f"分支切换失败: {result.error}")
            InfoBar.error(
                "失败", (result.error or "分支切换失败")[:150], parent=self
            )

        try:
            self.load_repo_info(repo, None)
        except ValueError:
            pass

        if dialog and result.success:
            dialog.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitManager()
    window.center_window()
    window.show()
    sys.exit(app.exec())
