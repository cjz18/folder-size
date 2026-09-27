"""Accept files dragged from Explorer onto a Tk window (Windows)."""

from __future__ import annotations

import sys
from collections.abc import Callable

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    _DROP_PROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _CallWindowProcW = _user32.CallWindowProcW
    _CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _CallWindowProcW.restype = ctypes.c_ssize_t
    _SetWindowLongPtrW = _user32.SetWindowLongPtrW
    _SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _SetWindowLongPtrW.restype = ctypes.c_void_p
    _kept: list[object] = []
    _shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragFinish.argtypes = [wintypes.HANDLE]
    _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]


def enable_file_drop(widget, callback: Callable[[list[str]], None]) -> None:
    if sys.platform != "win32":
        return
    widget.update_idletasks()
    hwnd = int(widget.winfo_id())
    parent = _user32.GetParent(hwnd)
    target = parent or hwnd
    if not target:
        return

    def proc(window, msg, wp, lp):
        if msg == WM_DROPFILES:
            count = _shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
            paths: list[str] = []
            for index in range(count):
                length = _shell32.DragQueryFileW(wp, index, None, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                _shell32.DragQueryFileW(wp, index, buf, length + 1)
                if buf.value:
                    paths.append(buf.value)
            _shell32.DragFinish(wp)
            if paths:
                widget.after(0, lambda found=paths: callback(found))
            return 0
        return _CallWindowProcW(old, window, msg, wp, lp)

    new_proc = _DROP_PROC(proc)
    old = _SetWindowLongPtrW(target, GWLP_WNDPROC, new_proc)
    _shell32.DragAcceptFiles(target, True)
    _kept.append((new_proc, old, target))
