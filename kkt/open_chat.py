"""Open a chat that has no window yet, through KakaoTalk's own UI and AX only.

The route follows openkakao-cli's `open_row_via_context_menu`
(https://github.com/JungHoonGhae/openkakao-cli, MIT, (c) 2026 Lucas):

1. KakaoTalk's search narrows the chat list. Setting `AXValue` does not filter,
   so the name goes in through `AXFocused` + `AXSelectedText`.
2. The one row whose name matches exactly gets `AXShowMenu` on its cell.
3. The context menu is not in the AX tree; it is found as an on-screen
   KakaoTalk window at a menu layer and hit-tested back to its `AXMenu`.
4. `채팅방 열기` is pressed with `AXPress`; a menu left behind gets `AXCancel`.

No keystrokes, no mouse events. The menu only appears while the main window is
on a Space being shown, so that is checked first instead of moving windows.
"""
from __future__ import annotations

import re
import subprocess
import time

import AppKit
import ApplicationServices as AS
import Quartz

from .ax import BUNDLE_ID, Chat, KakaoError, attr, frame, list_windows, nfc, _first

MAIN_TITLE = "카카오톡"
OPEN_ITEM_TITLES = ("채팅방 열기", "Open chat", "Open Chat")
# The chat list prefixes a room's kind to its name; window titles carry the name alone.
LIST_PREFIXES = ("나와의 채팅, ", "오픈채팅, ", "팀채팅, ")
NAME_LABEL_ID = "_NS:40"
RESTING_IDLE = 2.0   # seconds of no keyboard/mouse before a menu may be shown
OPEN_TIMEOUT = 5.0


def list_name(label: str) -> str:
    for p in LIST_PREFIXES:
        if label.startswith(p):
            return label[len(p):]
    return label


def hid_idle_seconds() -> float | None:
    out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-r", "-d", "1", "-k", "HIDIdleTime"],
                         capture_output=True, text=True, timeout=5).stdout
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else None


def mouse_location() -> tuple[float, float]:
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def menu_windows(pid: int) -> list[tuple[float, float, float, float]]:
    """On-screen KakaoTalk windows at a menu layer - where open context menus live."""
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) >= 100:
            b = w["kCGWindowBounds"]
            out.append((b["X"], b["Y"], b["Width"], b["Height"]))
    return out


def _menu_in_window(app, bounds):
    x, y, w, _ = bounds
    err, hit = AS.AXUIElementCopyElementAtPosition(app, x + w / 2, y + 12, None)
    if err != 0 or hit is None:
        return None
    for _ in range(8):
        if attr(hit, "AXRole") == "AXMenu":
            return hit
        hit = attr(hit, "AXParent")
        if hit is None:
            return None
    return None


def _wait_menu_closed(pid: int, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not menu_windows(pid):
            return True
        time.sleep(0.1)
    return False


def _main_cg_window(pid: int) -> dict | None:
    for w in list_windows(pid):
        if w["title"] == MAIN_TITLE:
            return w
    return None


def _frontmost() -> AppKit.NSRunningApplication:
    return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()


def open_chat(name: str, cache_path: str) -> dict:
    """Open `name` in its own window. Returns {"opened": title, "steps": [...]}
    or raises KakaoError with the step that refused."""
    name = nfc(name)
    steps: list[str] = []
    chat = Chat(name, cache_path)
    if chat.window() is not None:
        return {"opened": name, "already_open": True, "steps": ["window already open"]}
    pid = chat.pid
    main_cg = _main_cg_window(pid)
    if main_cg is None:
        raise KakaoError("KakaoTalk main window is not open")
    if not main_cg["onscreen"]:
        raise KakaoError("KakaoTalk's main window is on a desktop (Space) that is not being shown; "
                         "a context menu cannot open there. Switch to that desktop, or open the chat "
                         "window once by hand - sending to an open window works from any desktop.")
    main = Chat(MAIN_TITLE, cache_path).require_window()
    field = _first(main, lambda e: attr(e, "AXSubrole") == "AXSearchField")
    table = _first(main, lambda e: attr(e, "AXRole") == "AXTable")
    if field is None or table is None:
        raise KakaoError("main window is not on the chat list (search field or chat table missing)")
    if (attr(field, "AXValue") or "").strip():
        raise KakaoError("the main window's search field already has text (the user's own search); not touching it")
    if menu_windows(pid):
        raise KakaoError("a KakaoTalk menu is already open; not acting on it")
    app = AS.AXUIElementCreateApplication(pid)
    front_before = _frontmost()

    cancel = None
    try:
        before = len(attr(table, "AXRows") or [])
        AS.AXUIElementSetAttributeValue(field, "AXFocused", True)
        AS.AXUIElementSetAttributeValue(field, "AXValue", "")
        if AS.AXUIElementSetAttributeValue(field, "AXSelectedText", name) != 0:
            raise KakaoError("could not insert the search text")
        cancel = next((k for k in attr(field, "AXChildren") or [] if attr(k, "AXDescription") == "취소"), None)
        # Settled: the count left the full list's and read the same twice.
        deadline, last, count = time.time() + 3, None, before
        while time.time() < deadline:
            time.sleep(0.1)
            count = len(attr(table, "AXRows") or [])
            if count != before and count == last:
                break
            last = count
        else:
            raise KakaoError(f"KakaoTalk's search did not settle (rows {before} -> {count})")
        steps.append(f"search filtered {before} -> {count} rows")
        matches = []
        for row in attr(table, "AXRows") or []:
            cell = next((c for c in attr(row, "AXChildren") or [] if attr(c, "AXRole") == "AXCell"), None)
            label = _first(cell, lambda e: attr(e, "AXIdentifier") == NAME_LABEL_ID) if cell is not None else None
            if label is not None and list_name(nfc(attr(label, "AXValue"))) == name:
                matches.append(cell)
        if len(matches) != 1:
            raise KakaoError(f"{len(matches)} chat-list rows are named exactly {name!r}; need exactly one")
        idle = hid_idle_seconds()
        if idle is not None and idle < RESTING_IDLE:
            raise KakaoError(f"keyboard/mouse in use ({idle:.1f}s idle); a context menu now could catch the user's input")
        AS.AXUIElementPerformAction(matches[0], "AXShowMenu")  # may report an error and still open
        menu, deadline = None, time.time() + 1.5
        while menu is None and time.time() < deadline:
            time.sleep(0.03)
            for b in menu_windows(pid):
                menu = _menu_in_window(app, b)
                if menu is not None:
                    break
        if menu is None:
            leftover = "" if _wait_menu_closed(pid) else " (a KakaoTalk menu is showing but unreadable; click elsewhere to close it)"
            raise KakaoError("the row's context menu did not appear" + leftover)
        steps.append("context menu shown")
        items = [(it, nfc(attr(it, "AXTitle")).strip()) for it in attr(menu, "AXChildren") or []]
        targets = [it for it, t in items if t in OPEN_ITEM_TITLES]
        if len(targets) != 1:
            AS.AXUIElementPerformAction(menu, "AXCancel")
            raise KakaoError(f"context menu has no unique 채팅방 열기 item: {[t for _, t in items if t]}")
        mx, my = mouse_location()
        over = any(x <= mx <= x + w and y <= my <= y + h for x, y, w, h in menu_windows(pid))
        idle = hid_idle_seconds()
        if over and (idle is None or idle < RESTING_IDLE):
            AS.AXUIElementPerformAction(menu, "AXCancel")
            raise KakaoError("the cursor is over the menu and the mouse is in use; 채팅방 열기 was not pressed")
        err = AS.AXUIElementPerformAction(targets[0], "AXPress")
        steps.append(f"pressed 채팅방 열기 (err={err})")
        if not _wait_menu_closed(pid):
            AS.AXUIElementPerformAction(menu, "AXCancel")
        deadline = time.time() + OPEN_TIMEOUT
        while time.time() < deadline:
            if any(w["title"] == name for w in list_windows(pid)):
                break
            time.sleep(0.15)
        else:
            raise KakaoError(f"no window titled {name!r} appeared after 채팅방 열기")
        steps.append("chat window appeared")
    finally:
        if cancel is not None:
            AS.AXUIElementPerformAction(cancel, "AXPress")  # closes the search, restores the full list
        front_now = _frontmost()
        if front_now.bundleIdentifier() == BUNDLE_ID and front_before.bundleIdentifier() != BUNDLE_ID:
            front_before.activateWithOptions_(0)
            steps.append(f"KakaoTalk came to the front; handed focus back to {front_before.localizedName()}")
    return {"opened": name, "already_open": False, "steps": steps}
