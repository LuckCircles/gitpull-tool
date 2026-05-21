from ..wintypes import BOOL, HWND, INT, dll_import

SW_HIDE = 0
SW_SHOW = 5


@dll_import("user32")
def ShowWindow(wnd: HWND, nCmdShow: INT) -> None: ...


@dll_import("user32")
def IsWindowVisible(wnd: HWND) -> BOOL: ...
