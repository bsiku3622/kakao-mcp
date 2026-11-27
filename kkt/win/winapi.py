"""The Win32 calls the Windows backend needs, through ctypes only (no pywin32).

Everything acts on a window by handle: messages are sent or posted to that
window, so nothing depends on which app is in front, where the cursor is, or
whether the session is shown at all (a disconnected RDP session works). The
caller must run in the same Windows session as KakaoTalk - messages do not cross
sessions, so an SSH command (session 0) has to start it through an interactive
scheduled task.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as W

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# Per-monitor DPI awareness, so window rects, client coordinates for posted clicks
# and PrintWindow all speak physical pixels. A DPI-unaware process gets scaled-down
# rects and captured only the top-left part of KakaoTalk's window.
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2

LRESULT = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_CLOSE = 0x0010
VK_RETURN, VK_CONTROL, VK_SHIFT, VK_ESCAPE = 0x0D, 0x11, 0x10, 0x1B
MK_LBUTTON = 0x0001
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SMTO_ABORTIFHUNG = 0x0002
PW_RENDERFULLCONTENT = 0x00000002


class RECT(ctypes.Structure):
    _fields_ = [("left", W.LONG), ("top", W.LONG), ("right", W.LONG), ("bottom", W.LONG)]


def _sig(fn, args, res):
    fn.argtypes, fn.restype = args, res


_sig(user32.EnumWindows, [WNDENUMPROC, W.LPARAM], W.BOOL)
_sig(user32.EnumChildWindows, [W.HWND, WNDENUMPROC, W.LPARAM], W.BOOL)
_sig(user32.GetClassNameW, [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.GetWindowTextW, [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.GetWindowTextLengthW, [W.HWND], ctypes.c_int)
_sig(user32.IsWindowVisible, [W.HWND], W.BOOL)
_sig(user32.IsWindow, [W.HWND], W.BOOL)
_sig(user32.GetParent, [W.HWND], W.HWND)
_sig(user32.GetWindowRect, [W.HWND, ctypes.POINTER(RECT)], W.BOOL)
_sig(user32.GetClientRect, [W.HWND, ctypes.POINTER(RECT)], W.BOOL)
_sig(user32.GetWindowThreadProcessId, [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD)
_sig(user32.SendMessageW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM], LRESULT)
_sig(user32.SendMessageTimeoutW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t)], LRESULT)
_sig(user32.PostMessageW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL)
_sig(user32.MapVirtualKeyW, [W.UINT, W.UINT], W.UINT)
_sig(user32.AttachThreadInput, [W.DWORD, W.DWORD, W.BOOL], W.BOOL)
_sig(user32.GetKeyboardState, [ctypes.POINTER(ctypes.c_ubyte)], W.BOOL)
_sig(user32.SetKeyboardState, [ctypes.POINTER(ctypes.c_ubyte)], W.BOOL)
_sig(user32.GetForegroundWindow, [], W.HWND)
_sig(user32.OpenClipboard, [W.HWND], W.BOOL)
_sig(user32.CloseClipboard, [], W.BOOL)
_sig(user32.EmptyClipboard, [], W.BOOL)
_sig(user32.GetClipboardData, [W.UINT], W.HANDLE)
_sig(user32.SetClipboardData, [W.UINT, W.HANDLE], W.HANDLE)
_sig(user32.GetClipboardSequenceNumber, [], W.DWORD)
_sig(user32.GetWindowDC, [W.HWND], W.HDC)
_sig(user32.ReleaseDC, [W.HWND, W.HDC], ctypes.c_int)
_sig(user32.PrintWindow, [W.HWND, W.HDC, W.UINT], W.BOOL)
_sig(kernel32.GetCurrentThreadId, [], W.DWORD)
_sig(kernel32.GlobalAlloc, [W.UINT, ctypes.c_size_t], W.HGLOBAL)
_sig(kernel32.GlobalLock, [W.HGLOBAL], W.LPVOID)
_sig(kernel32.GlobalUnlock, [W.HGLOBAL], W.BOOL)
_sig(kernel32.OpenProcess, [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
_sig(kernel32.CloseHandle, [W.HANDLE], W.BOOL)
_sig(kernel32.QueryFullProcessImageNameW, [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL)
_sig(kernel32.ProcessIdToSessionId, [W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL)
_sig(kernel32.GetCurrentProcessId, [], W.DWORD)
_sig(gdi32.CreateCompatibleDC, [W.HDC], W.HDC)
_sig(gdi32.CreateCompatibleBitmap, [W.HDC, ctypes.c_int, ctypes.c_int], W.HBITMAP)
_sig(gdi32.SelectObject, [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
_sig(gdi32.DeleteObject, [W.HGDIOBJ], W.BOOL)
_sig(gdi32.DeleteDC, [W.HDC], W.BOOL)
_sig(gdi32.GetDIBits, [W.HDC, W.HBITMAP, W.UINT, W.UINT, W.LPVOID, W.LPVOID, W.UINT], ctypes.c_int)


# -- windows -----------------------------------------------------------------
def class_name(h) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def title(h) -> str:
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value


def visible(h) -> bool:
    return bool(user32.IsWindowVisible(h))


def alive(h) -> bool:
    return bool(h) and bool(user32.IsWindow(h))


def rect(h) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def client_size(h) -> tuple[int, int]:
    r = RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def parent(h):
    return user32.GetParent(h)


def pid_of(h) -> int:
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return pid.value


def thread_of(h) -> int:
    return user32.GetWindowThreadProcessId(h, None)


def image_name(pid: int) -> str:
    hp = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(hp)


def session_of(pid: int) -> int:
    sid = W.DWORD()
    kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
    return sid.value


def top_windows() -> list:
    out = []
    cb = WNDENUMPROC(lambda h, _: out.append(h) or True)
    user32.EnumWindows(cb, 0)
    return out


def descendants(h) -> list:
    out = []
    cb = WNDENUMPROC(lambda c, _: out.append(c) or True)
    user32.EnumChildWindows(h, cb, 0)
    return out


def first_descendant(h, cls: str, need_visible: bool = False):
    for c in descendants(h):
        if class_name(c) == cls and (visible(c) or not need_visible):
            return c
    return None


# -- text controls -----------------------------------------------------------
def send(h, msg, wparam=0, lparam=0, timeout_ms: int = 5000) -> int:
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(h, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    return result.value


def get_text(h) -> str:
    n = send(h, WM_GETTEXTLENGTH)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.SendMessageW(h, WM_GETTEXT, n + 2, ctypes.cast(buf, ctypes.c_void_p).value)
    return buf.value


def set_text(h, text: str) -> bool:
    buf = ctypes.create_unicode_buffer(text)
    return bool(user32.SendMessageW(h, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value))


def type_chars(h, text: str) -> None:
    for ch in text:
        user32.SendMessageW(h, WM_CHAR, ord(ch), 0)


# -- keys to one window (no real input) --------------------------------------
def _key_lparam(vk: int, up: bool) -> int:
    scan = user32.MapVirtualKeyW(vk, 0)
    lp = 1 | (scan << 16)
    if up:
        lp |= 0xC0000000
    return lp


def post_key(h, vk: int, modifiers: tuple[int, ...] = ()) -> None:
    """Post vk (with Ctrl/Shift held in that thread's key state only) to window h.

    Modifier state is per thread: attach to the window's thread, mark the
    modifiers down in the shared key-state table, post the key, then put the
    table back. The physical keyboard and the foreground app are not involved.
    """
    if not modifiers:
        user32.PostMessageW(h, WM_KEYDOWN, vk, _key_lparam(vk, False))
        user32.PostMessageW(h, WM_KEYUP, vk, _key_lparam(vk, True))
        return
    me, target = kernel32.GetCurrentThreadId(), thread_of(h)
    attached = bool(user32.AttachThreadInput(me, target, True))
    try:
        old = (ctypes.c_ubyte * 256)()
        user32.GetKeyboardState(old)
        new = (ctypes.c_ubyte * 256)(*old)
        for m in modifiers:
            new[m] = 0x80
        user32.SetKeyboardState(new)
        time.sleep(0.02)
        user32.PostMessageW(h, WM_KEYDOWN, vk, _key_lparam(vk, False))
        time.sleep(0.02)
        user32.PostMessageW(h, WM_KEYUP, vk, _key_lparam(vk, True))
        time.sleep(0.05)
        user32.SetKeyboardState(old)
    finally:
        if attached:
            user32.AttachThreadInput(me, target, False)


def post_click(h, x: int, y: int, double: bool = False) -> None:
    """Post a left click at client coordinates (x, y) of h - a message, not a cursor move."""
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(h, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    user32.PostMessageW(h, WM_LBUTTONUP, 0, lp)
    if double:
        user32.PostMessageW(h, WM_LBUTTONDBLCLK, MK_LBUTTON, lp)
        user32.PostMessageW(h, WM_LBUTTONUP, 0, lp)


# -- clipboard ---------------------------------------------------------------
def _open_clipboard(tries: int = 20) -> bool:
    for _ in range(tries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str | None) -> None:
    if not _open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        if text is None:
            return
        data = (text + "\0").encode("utf-16-le")
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        user32.CloseClipboard()


def clipboard_seq() -> int:
    return user32.GetClipboardSequenceNumber()


# -- capture -----------------------------------------------------------------
def capture_png(h, path: str) -> str:
    """PrintWindow into a bitmap - works for windows behind others or off the shown desktop."""
    from PIL import Image
    w, hgt = rect(h)[2:]
    hdc = user32.GetWindowDC(h)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, hgt)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        user32.PrintWindow(h, mdc, PW_RENDERFULLCONTENT)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                        ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                        ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                        ("biClrImportant", W.DWORD)]
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -hgt, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * hgt * 4)
        gdi32.GetDIBits(mdc, bmp, 0, hgt, buf, ctypes.byref(bi), 0)
        Image.frombuffer("RGB", (w, hgt), buf, "raw", "BGRX", 0, 1).save(path)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(h, hdc)
    return path
