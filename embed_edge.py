# -*- coding: utf-8 -*-
"""Chrome 窗口内嵌：把调试模式的 Chrome 窗口嵌入 tkinter Frame（Win32 SetParent）。
用于「浏览器模式」，内嵌后 Edge 不弹独立窗口，油猴脚本/登录态照常生效。"""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_APPWINDOW = 0x00040000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SW_SHOW = 5
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _get_window_long(hwnd, idx):
    try:
        return user32.GetWindowLongPtrW(wt.HWND(hwnd), idx)
    except AttributeError:
        return user32.GetWindowLongW(wt.HWND(hwnd), idx)


def _set_window_long(hwnd, idx, val):
    try:
        return user32.SetWindowLongPtrW(wt.HWND(hwnd), idx, val)
    except AttributeError:
        return user32.SetWindowLongW(wt.HWND(hwnd), idx, val)


def _is_chrome(pid):
    """按 PID 判断进程是否为 chrome.exe"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.lower().endswith('chrome.exe')
        return False
    finally:
        kernel32.CloseHandle(h)


def find_edge_window(timeout=20):
    """等待并返回调试 Chrome 的顶层主窗口 HWND；超时返回 None。
    匹配条件：类名 Chrome_WidgetWin_1 + 可见 + 顶层 + chrome 进程。"""
    deadline = time.time() + timeout
    found = []

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != 'Chrome_WidgetWin_1':
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetParent(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _is_chrome(pid.value):
            found.append(hwnd)
        return True

    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def embed_edge(hwnd_edge, hwnd_host, x=0, y=0, w=800, h=600):
    """把 Chrome 窗口设为 host 的子窗口：去系统边框、随宿主缩放"""
    user32.SetParent(wt.HWND(hwnd_edge), wt.HWND(hwnd_host))
    style = _get_window_long(hwnd_edge, GWL_STYLE)
    style = (style | WS_CHILD) & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME
                                   | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
    _set_window_long(hwnd_edge, GWL_STYLE, style)
    ex = _get_window_long(hwnd_edge, GWL_EXSTYLE)
    _set_window_long(hwnd_edge, GWL_EXSTYLE, ex & ~WS_EX_APPWINDOW)
    user32.SetWindowPos(wt.HWND(hwnd_edge), 0, x, y, w, h,
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
    user32.ShowWindow(wt.HWND(hwnd_edge), SW_RESTORE)  # 若以最小化启动，先恢复再显示


def resize_edge(hwnd_edge, x, y, w, h):
    """随宿主窗口尺寸变化同步调整 Chrome 窗口"""
    user32.MoveWindow(wt.HWND(hwnd_edge), x, y, w, h, True)
