"""设置页面widget"""

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    PrimaryPushButton,
    PushButton,
    PushSettingCard,
    SettingCardGroup,
    SwitchSettingCard,
)


class SettingsWidget(QWidget):
    """设置页面widget"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        """初始化设置页面UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(20)

        # ========== 存储设置 ==========
        self.dir_group = SettingCardGroup("存储设置", self)
        self.dir_card = PushSettingCard(
            "选择目录",
            FIF.FOLDER,
            "仓库存放目录",
            "所有 Git 仓库将被存放在此目录下",
            self.dir_group,
        )
        self.dir_group.addSettingCard(self.dir_card)
        layout.addWidget(self.dir_group)

        # ========== 网络设置 ==========
        self.network_group = SettingCardGroup("网络设置", self)

        self.proxy_card = PushSettingCard(
            "点击编辑",
            FIF.GLOBE,
            "Git代理地址",
            "用于加速 Git 克隆和拉取，如 http://127.0.0.1:7897",
            self.network_group,
        )
        self.network_group.addSettingCard(self.proxy_card)

        # 验证代理卡片
        self.verify_proxy_card = PushSettingCard(
            "验证连接",
            FIF.PLAY,
            "验证代理",
            "验证代理配置和 GitHub 连接性",
            self.network_group,
        )
        self.network_group.addSettingCard(self.verify_proxy_card)

        self.proxy_switch = SwitchSettingCard(
            FIF.POWER_BUTTON,
            "启用代理",
            content="关闭后 Git 操作将不使用代理",
            parent=self.network_group,
        )
        self.proxy_switch.setChecked(True)
        self.network_group.addSettingCard(self.proxy_switch)

        self.token_card = PushSettingCard(
            "点击编辑",
            FIF.HEART,
            "访问令牌",
            "用于私有仓库认证，支持 GitHub / Gitee Token",
            self.network_group,
        )
        self.network_group.addSettingCard(self.token_card)

        layout.addWidget(self.network_group)

        # ========== 关于 ==========
        self.about_group = SettingCardGroup("关于", self)
        self.about_card = PushSettingCard(
            "查看",
            FIF.INFO,
            "Git 多仓库管理器",
            "高级版 · 支持批量管理、分支切换、版本回退",
            self.about_group,
        )
        self.about_group.addSettingCard(self.about_card)
        layout.addWidget(self.about_group)

        # ========== 底部操作 ==========
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 8, 0, 0)

        self.save_btn = PrimaryPushButton(FIF.SAVE, "保存设置")
        self.save_btn.setMinimumWidth(160)

        self.reset_btn = PushButton(FIF.CANCEL, "重置")
        self.reset_btn.setMinimumWidth(100)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.reset_btn)
        bottom_layout.addWidget(self.save_btn)
        layout.addWidget(bottom_widget)

        # 添加拉伸以填充剩余空间
        layout.addStretch(1)
