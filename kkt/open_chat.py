"""Open a chat that has no window yet, through KakaoTalk's own UI and AX only.

The route follows openkakao-cli's `open_row_via_context_menu`
(https://github.com/JungHoonGhae/openkakao-cli, MIT, (c) 2026 Lucas):

1. KakaoTalk's search narrows the chat list. Setting `AXValue` does not filter,
   so the name goes in through `AXFocused` + `AXSelectedText`.
2. The one row whose name matches exactly gets `AXShowMenu` on its cell.
3. The context menu is not in the AX tree; it is found as an on-screen
   KakaoTalk window at a menu layer and hit-tested back to its `AXMenu`.
4. `채팅방 열기` is pressed with `AXPress`; a menu left behind gets `AXCancel`.

No keystrokes, no mouse events. Measured on KakaoTalk 26.10.0: chat rows, cells
and their buttons support no other action (AXPress, AXOpen, AXConfirm,
AXShowDefaultUI all return -25205/-25206), and AXConfirm on the search field
opens nothing, so the context menu is the only element-scoped way in.

That menu only appears while the main window is on a Space being shown. When it
is not, KakaoTalk is brought forward with AXFrontmost (macOS then shows its
Space), the chat is opened, and the app that was in front is handed back the
same way - about a second, only for a room that has no window yet. This waits
for the keyboard and mouse to rest first, so typing never lands in KakaoTalk.
"""
from __future__ import annotations

import re
import subprocess
import time

import ApplicationServices as AS
import Quartz

from .ax import Chat, KakaoError, attr, list_windows, nfc, _first

MAIN_TITLE = "카카오톡"
OPEN_ITEM_TITLES = ("채팅방 열기", "Open chat", "Open Chat")
# The chat list prefixes a room's kind to its name; window titles carry the name alone.
LIST_PREFIXES = ("나와의 채팅, ", "오픈채팅, ", "팀채팅, ")
NAME_LABEL_ID = "_NS:40"
RESTING_IDLE = 1.5   # seconds of no keyboard/mouse before KakaoTalk may come forward
IDLE_WAIT = 30.0     # how long to wait for that rest before giving up
OPEN_TIMEOUT = 6.0


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


def focused_pid() -> int | None:
    """The app with keyboard focus, from AX (NSWorkspace goes stale without a run loop)."""
    err, app = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
    if err != 0 or app is None:
        return None
    err, pid = AS.AXUIElementGetPid(app, None)
    return pid if err == 0 else None


def _set_frontmost(pid: int) -> None:
    AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", True)


def _wait(cond, timeout: float, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def _wait_idle(seconds: float = RESTING_IDLE, timeout: float = IDLE_WAIT) -> None:
    if not _wait(lambda: (hid_idle_seconds() or seconds) >= seconds, timeout, 0.25):
        raise KakaoError(f"the keyboard or mouse stayed in use for {timeout:g}s; KakaoTalk was not brought "
                         "forward so typing cannot land in it - try again in a moment")


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
    return _wait(lambda: not menu_windows(pid), timeout, 0.1)


def _main_on_screen(pid: int) -> bool:
    return any(w["title"] == MAIN_TITLE and w["onscreen"] for w in list_windows(pid))


def _press_window_menu(pid: int, title: str) -> bool:
    """Press 창 > <title> in KakaoTalk's menu bar through AX (no menu is drawn)."""
    bar = attr(AS.AXUIElementCreateApplication(pid), "AXMenuBar")
    top = next((c for c in attr(bar, "AXChildren") or [] if nfc(attr(c, "AXTitle")) in ("창", "Window")), None)
    menu = next((c for c in attr(top, "AXChildren") or [] if attr(c, "AXRole") == "AXMenu"), None) if top else None
    item = next((i for i in attr(menu, "AXChildren") or [] if nfc(attr(i, "AXTitle")) == title), None) if menu else None
    return item is not None and AS.AXUIElementPerformAction(item, "AXPress") == 0


def _filtered_rows(field, table, name: str) -> tuple[list, int, int]:
    before = len(attr(table, "AXRows") or [])
    AS.AXUIElementSetAttributeValue(field, "AXFocused", True)
    AS.AXUIElementSetAttributeValue(field, "AXValue", "")
    if AS.AXUIElementSetAttributeValue(field, "AXSelectedText", name) != 0:
        raise KakaoError("could not insert the search text")
    # Settled: the count left the full list's and read the same twice.
    last, count, end = None, before, time.time() + 3
    while time.time() < end:
        time.sleep(0.1)
        count = len(attr(table, "AXRows") or [])
        if count != before and count == last:
            break
        last = count
    else:
        raise KakaoError(f"KakaoTalk's search did not settle (rows {before} -> {count})")
    return attr(table, "AXRows") or [], before, count


def _open_via_menu(pid: int, cell, steps: list[str]) -> None:
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementPerformAction(cell, "AXShowMenu")  # may report an error and still open
    def shown_menu():
        # Measured on 26.10.0: the AXMenu is a child of the cell, and hit-testing the menu
        # window lands on the cell underneath, so the tree is checked first.
        for parent in (cell, app):
            for ch in attr(parent, "AXChildren") or []:
                if attr(ch, "AXRole") == "AXMenu":
                    return ch
        for b in menu_windows(pid):
            m = _menu_in_window(app, b)
            if m is not None:
                return m
        return None

    menu, end = None, time.time() + 1.5
    while menu is None and time.time() < end:
        time.sleep(0.03)
        menu = shown_menu()
    if menu is None:
        leftover = "" if _wait_menu_closed(pid) else " (a KakaoTalk menu is showing but unreadable; click elsewhere to close it)"
        raise KakaoError("the row's context menu did not appear" + leftover)
    items = [(it, nfc(attr(it, "AXTitle")).strip()) for it in attr(menu, "AXChildren") or []]
    targets = [it for it, t in items if t in OPEN_ITEM_TITLES]
    if len(targets) != 1:
        AS.AXUIElementPerformAction(menu, "AXCancel")
        raise KakaoError(f"context menu has no unique 채팅방 열기 item: {[t for _, t in items if t]}")
    mx, my = mouse_location()
    over = any(x <= mx <= x + w and y <= my <= y + h for x, y, w, h in menu_windows(pid))
    if over and (hid_idle_seconds() or 0) < RESTING_IDLE:
        AS.AXUIElementPerformAction(menu, "AXCancel")
        raise KakaoError("the cursor is over the menu and the mouse is in use; 채팅방 열기 was not pressed")
    err = AS.AXUIElementPerformAction(targets[0], "AXPress")
    steps.append(f"pressed 채팅방 열기 (err={err})")
    if not _wait_menu_closed(pid):
        AS.AXUIElementPerformAction(menu, "AXCancel")


def open_chat(name: str, cache_path: str) -> dict:
    """Open `name` in its own window. Returns {"opened", "already_open", "switched", "steps"}
    or raises KakaoError naming the step that refused."""
    name = nfc(name)
    steps: list[str] = []
    chat = Chat(name, cache_path)
    if chat.window() is not None:
        return {"opened": name, "already_open": True, "switched": False, "steps": ["window already open"]}
    pid = chat.pid
    if not any(w["title"] == MAIN_TITLE for w in list_windows(pid)):
        if not _press_window_menu(pid, "채팅"):
            raise KakaoError("KakaoTalk's main window is closed and 창 > 채팅 could not reopen it")
        _wait(lambda: any(w["title"] == MAIN_TITLE for w in list_windows(pid)), 3)
        steps.append("reopened the main window (창 > 채팅)")
    main = Chat(MAIN_TITLE, cache_path).require_window()
    field = _first(main, lambda e: attr(e, "AXSubrole") == "AXSearchField")
    table = _first(main, lambda e: attr(e, "AXRole") == "AXTable")
    if field is None or table is None:   # another tab (friends, more) is showing
        _press_window_menu(pid, "채팅")
        time.sleep(0.4)
        field = _first(main, lambda e: attr(e, "AXSubrole") == "AXSearchField")
        table = _first(main, lambda e: attr(e, "AXRole") == "AXTable")
        if field is None or table is None:
            raise KakaoError("could not show the chat list in the main window")
        steps.append("switched the main window to the chat list")
    if (attr(field, "AXValue") or "").strip():
        raise KakaoError("the main window's search field already has text (the user's own search); not touching it")
    if menu_windows(pid):
        raise KakaoError("a KakaoTalk menu is already open; not acting on it")

    cancel, front_before, switched = None, focused_pid(), False
    try:
        rows, before, count = _filtered_rows(field, table, name)
        cancel = next((k for k in attr(field, "AXChildren") or [] if attr(k, "AXDescription") == "취소"), None)
        steps.append(f"search filtered {before} -> {count} rows")
        matches, seen = [], []
        for row in rows:
            cell = next((c for c in attr(row, "AXChildren") or [] if attr(c, "AXRole") == "AXCell"), None)
            label = _first(cell, lambda e: attr(e, "AXIdentifier") == NAME_LABEL_ID) if cell is not None else None
            if label is None:
                continue
            listed = list_name(nfc(attr(label, "AXValue")))
            seen.append(listed)
            if listed == name:
                matches.append(cell)
        if len(matches) != 1:
            hint = f" Rooms the search found: {seen[:8]}" if seen else " KakaoTalk's search found no room."
            raise KakaoError(f"{len(matches)} chat-list rows are named exactly {name!r}; need exactly one "
                             f"(use the name as the chat list shows it, without a '팀채팅, ' prefix).{hint}")
        if not _main_on_screen(pid):
            _wait_idle()
            front_before = focused_pid()
            _set_frontmost(pid)
            switched = True
            if not _wait(lambda: _main_on_screen(pid), 3):
                raise KakaoError("KakaoTalk came forward but its main window did not show")
            time.sleep(0.45)   # let the desktop-switch animation finish before a menu is drawn
            steps.append("brought KakaoTalk's desktop forward")
        _open_via_menu(pid, matches[0], steps)
        if not _wait(lambda: any(w["title"] == name for w in list_windows(pid)), OPEN_TIMEOUT, 0.15):
            raise KakaoError(f"no window titled {name!r} appeared after 채팅방 열기")
        steps.append("chat window appeared")
    finally:
        if cancel is not None:
            AS.AXUIElementPerformAction(cancel, "AXPress")  # closes the search, restores the full list
        # KakaoTalk may take the front again while it shows the new window, with or
        # without a desktop switch, so the hand-back is checked and repeated.
        if front_before and front_before != pid and (switched or focused_pid() == pid):
            for _ in range(4):
                time.sleep(0.3)
                _set_frontmost(front_before)
                if _wait(lambda: focused_pid() == front_before, 1.5) and \
                        not _wait(lambda: focused_pid() != front_before, 0.6):
                    steps.append("handed the front back")
                    break
            else:
                steps.append("could not hand the front back")
    return {"opened": name, "already_open": False, "switched": switched, "steps": steps}
