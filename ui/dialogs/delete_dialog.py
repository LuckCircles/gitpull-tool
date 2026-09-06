"""删除仓库对话框 - Fluent 遮罩弹窗"""

import os

from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    MessageBoxBase,
    RadioButton,
    StrongBodyLabel,
)

from ui.widgets.dark_window import MaskFadeGuardMixin, apply_tooltip


class DeleteRepoDialog(MaskFadeGuardMixin, MessageBoxBase):
    """删除仓库对话框（Fluent 遮罩弹窗）。

    支持:
        1. 删除整个仓库目录
        2. 仅删除 .git 文件夹
    """

    DELETE_ALL = 0
    DELETE_GIT_ONLY = 1

    def __init__(self, repo_path: str, parent=None):
        super().__init__(parent)

        self.repo_path = os.path.abspath(repo_path)

        self.repo_name = os.path.basename(self.repo_path.rstrip("\\/"))

        self.delete_mode = self.DELETE_ALL

        self.widget.setFixedWidth(480)

        self.viewLayout.addWidget(StrongBodyLabel("删除仓库"))

        # 仓库信息
        info = BodyLabel(f"""
仓库名称:
{self.repo_name}

路径:
{self.repo_path}
""")
        info.setWordWrap(True)
        self.viewLayout.addWidget(info)

        # 删除模式
        self.viewLayout.addWidget(StrongBodyLabel("删除方式"))

        self.full_delete_radio = RadioButton("完全删除")
        self.full_delete_radio.setChecked(True)
        self.viewLayout.addWidget(self.full_delete_radio)

        desc1 = BodyLabel("删除整个仓库目录，包括所有代码文件")
        desc1.setStyleSheet("color:#888;")
        self.viewLayout.addWidget(desc1)

        self.git_only_radio = RadioButton("仅删除 Git 信息")
        self.viewLayout.addWidget(self.git_only_radio)

        desc2 = BodyLabel("保留代码文件，后续不会继续扫描仓库")
        desc2.setStyleSheet("color:#888;")
        self.viewLayout.addWidget(desc2)

        # 警告
        warning = BodyLabel("⚠ 此操作不可恢复，请确认后继续")
        warning.setStyleSheet("color:#d13438;")
        self.viewLayout.addWidget(warning)

        self.yesButton.setText("确认")
        apply_tooltip(self.yesButton, "确认按所选方式删除仓库（不可恢复）")
        self.cancelButton.setText("取消")
        apply_tooltip(self.cancelButton, "取消删除并关闭窗口")

    def validate(self) -> bool:
        """点击确认时记录所选删除模式。"""
        if not self.repo_path:
            InfoBar.error("错误", "仓库路径为空", parent=self)
            return False

        if self.full_delete_radio.isChecked():
            self.delete_mode = self.DELETE_ALL
        else:
            self.delete_mode = self.DELETE_GIT_ONLY
        return True

    def get_delete_mode(self):
        return self.delete_mode

    def is_delete_all(self):
        return self.delete_mode == self.DELETE_ALL

    def is_delete_git_only(self):
        return self.delete_mode == self.DELETE_GIT_ONLY
