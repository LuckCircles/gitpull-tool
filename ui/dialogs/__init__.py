"""UI dialogs package"""

from ui.dialogs.branch_dialog import BranchDialog
from ui.dialogs.clone_dialog import CloneRepoDialog
from ui.dialogs.delete_dialog import DeleteRepoDialog
from ui.dialogs.history_dialog import HistoryDialog
from ui.dialogs.rename_dialog import RenameRepoDialog

__all__ = [
    "BranchDialog",
    "CloneRepoDialog",
    "DeleteRepoDialog",
    "HistoryDialog",
    "RenameRepoDialog",
]
