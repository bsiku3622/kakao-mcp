"""KakaoTalk for Windows through window messages - the Windows counterpart of kkt.ax.

Measured on KakaoTalk for Windows 26.10.0 (2026-11-27). Nothing is brought to the
front and the real keyboard and mouse are never used; every step is a message to
one KakaoTalk window, which works with the main window hidden in the tray and in a
disconnected RDP session. The process must run in KakaoTalk's Windows session.

- Open: Ctrl+F (see winapi.post_key) shows a list's search box, WM_SETTEXT fills it,
  and Enter opens the top result. The friends tab is tried first - it matches friend
  names only, so a 1:1 room comes up alone - then the chat tab. A window whose title
  is not exactly the requested name is closed again. The search result lists have no
  readable text (custom-drawn, no MSAA rows), so the title is the only check.
- Send: WM_SETTEXT into RICHEDIT50W, then Enter with VK_RETURN marked down in the
  thread's key state. A plain posted Enter does nothing, a sent one only inserts a
  line break, and the key-state Enter sends only after a synchronous Enter on the
  empty composer has primed it - so every send primes first.
- Read: Ctrl+A, Ctrl+C to the message list, then the clipboard, which is put back.
  Lines read "[sender] [오전|오후 H:MM] text". Messages of about 500+ characters come
  back cut and ending in "...".
- The composer shows its placeholder "메시지 입력" as real text.
"""
from __future__ import annotations

import os
import re
import time

from ..textmatch import normalize, same_text
from . import winapi as w

MAIN_CLASS, MAIN_TITLE = "EVA_Window_Dblclk", "카카오톡"
COMPOSER, MESSAGE_LIST = "RICHEDIT50W", "EVA_VH_ListControl_Dblclk"
PLACEHOLDER = "메시지 입력"
TAIL = 15

_HEAD = re.compile(r"^\[(.+?)\] \[(오전|오후) (\d{1,2}:\d{2})\] ?(.*)$")
_DATE = re.compile(r"^\d{4}년 \d{1,2}월 \d{1,2}일 \S+요일$")
_SYSTEM = re.compile(r"(님이 들어왔습니다|님이 나갔습니다|님을 초대했습니다|님을 내보냈습니다|"
                     r"메시지가 삭제되었습니다|관리자가 메시지를 가렸습니다)\.?$")


class KakaoError(RuntimeError):
    pass


# -- windows -----------------------------------------------------------------
def kakao_pid() -> int | None:
    for h in w.top_windows():
        if w.class_name(h) == MAIN_CLASS and w.title(h) == MAIN_TITLE:
            return w.pid_of(h)
    return None


def _kakao_tops(pid: int) -> list:
    return [h for h in w.top_windows() if w.pid_of(h) == pid]


def main_window():
    pid = kakao_pid()
    if pid is None:
        raise KakaoError("KakaoTalk is not running in this Windows session")
    return next(h for h in _kakao_tops(pid) if w.class_name(h) == MAIN_CLASS and w.title(h) == MAIN_TITLE)


def logged_in() -> bool:
    """Signed out, KakaoTalk shows a separate login window (EVA_Window "카카오톡" with edits)."""
    pid = kakao_pid()
    if pid is None:
        return False
    return not any(w.class_name(h) == "EVA_Window" and w.title(h) == MAIN_TITLE and w.visible(h)
                   and w.first_descendant(h, "Edit") for h in _kakao_tops(pid))


def rooms() -> list[tuple[int, str]]:
    """Open chat windows: titled KakaoTalk windows that have a composer."""
    pid = kakao_pid()
    if pid is None:
        return []
    return [(h, w.title(h)) for h in _kakao_tops(pid)
            if w.class_name(h) == MAIN_CLASS and w.visible(h) and w.title(h) not in ("", MAIN_TITLE)
            and w.first_descendant(h, COMPOSER)]


def room_window(name: str):
    return next((h for h, t in rooms() if t == name), None)


def _blank(text: str) -> bool:
    return text.strip() in ("", PLACEHOLDER)


# -- reading -----------------------------------------------------------------
def copy_all(room) -> str:
    """The loaded conversation as KakaoTalk copies it. The clipboard is put back."""
    lst = w.first_descendant(room, MESSAGE_LIST)
    if lst is None:
        raise KakaoError("message list not found in the chat window")
    saved, seq = w.clipboard_text(), w.clipboard_seq()
    try:
        w.post_key(lst, ord("A"), (w.VK_CONTROL,))
        time.sleep(0.15)
        w.post_key(lst, ord("C"), (w.VK_CONTROL,))
        end = time.time() + 3
        while w.clipboard_seq() == seq and time.time() < end:
            time.sleep(0.05)
        if w.clipboard_seq() == seq:
            raise KakaoError("KakaoTalk did not copy the conversation")
        return w.clipboard_text() or ""
    finally:
        w.set_clipboard_text(saved)


def parse(raw: str, me: set[str]) -> list[dict]:
    msgs: list[dict] = []
    cur = None
    for line in raw.replace("\r\n", "\n").split("\n"):
        m = _HEAD.match(line)
        if m:
            cur = {"dir": "out" if m.group(1) in me else "in", "sender": m.group(1),
                   "time": f"{m.group(2)} {m.group(3)}", "text": m.group(4)}
            msgs.append(cur)
            continue
        s = line.strip()
        if _DATE.match(s) or _SYSTEM.search(s):
            cur = None
        elif cur is not None:
            cur["text"] += "\n" + line
    for m in msgs:
        m["text"] = m["text"].rstrip("\n")
        m["folded"] = m["text"].endswith("...")
    return msgs


def read(room, me: set[str], limit: int = 20) -> list[dict]:
    return parse(copy_all(room), me)[-limit:]


# -- sending -----------------------------------------------------------------
def _prime(e) -> None:
    w.set_text(e, "")
    w.user32.SendMessageW(e, w.WM_KEYDOWN, w.VK_RETURN, w._key_lparam(w.VK_RETURN, False))
    w.user32.SendMessageW(e, w.WM_KEYUP, w.VK_RETURN, w._key_lparam(w.VK_RETURN, True))
    w.set_text(e, "")


def _wait(cond, timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def deliver(room, text: str, me: set[str], allow_repeat: bool = False, verify: float = 6.0) -> dict:
    """Same verdicts as kkt.deliver on the Mac: sent (bubble | composer_cleared), not_sent."""
    e = w.first_descendant(room, COMPOSER)
    if e is None:
        raise KakaoError("composer not found in the chat window")
    draft = w.get_text(e)
    if not _blank(draft):
        raise KakaoError(f"the composer already holds a draft; not overwriting it ({len(draft)} chars)")
    sent_n = normalize(text)
    outs = [m for m in read(room, me, TAIL) if m["dir"] == "out"]
    if not allow_repeat and outs and same_text(outs[-1]["text"], sent_n):
        raise KakaoError("the newest outgoing message in this room already shows this text, so it was "
                         "probably sent already; pass allow_repeat=true to send it again")
    before = sum(1 for m in outs if same_text(m["text"], sent_n))
    for attempt in (1, 2):
        _prime(e)
        w.set_text(e, text.replace("\r\n", "\n").replace("\n", "\r\n"))
        w.post_key(e, w.VK_RETURN, (w.VK_RETURN,))
        if _wait(lambda: _blank(w.get_text(e)), 3.0):
            break
    else:
        w.set_text(e, "")
        return {"status": "not_sent", "detail": "Enter did not send; composer cleared"}
    t0 = time.time()
    while time.time() - t0 < verify:
        time.sleep(0.5)
        try:
            mine = [m for m in read(room, me, TAIL + 5) if m["dir"] == "out"]
        except KakaoError:
            continue
        if sum(1 for m in mine if same_text(m["text"], sent_n)) > before:
            return {"status": "sent", "verified_by": "message",
                    "detail": f"seen in the copied conversation after {time.time() - t0:.1f}s (attempt {attempt})"}
    return {"status": "sent", "verified_by": "composer_cleared",
            "detail": "KakaoTalk cleared the composer; the message was not matched in the copied conversation. "
                      "Do not resend."}


# -- opening -----------------------------------------------------------------
def open_room(name: str, timeout: float = 4.0) -> dict:
    if room_window(name):
        return {"opened": name, "already_open": True, "steps": ["window already open"]}
    main = main_window()
    pid = w.pid_of(main)
    steps, closed = [], []
    for view_name, label in (("ContactListView", "friends"), ("ChatRoomListView", "chats")):
        view = next((c for c in w.descendants(main) if w.title(c).startswith(view_name)), None)
        edit = next((c for c in w.descendants(view) if w.class_name(c) == "Edit" and w.parent(c) == view), None) if view else None
        if edit is None:
            steps.append(f"{label}: search box not found")
            continue
        before = set(_kakao_tops(pid))
        w.post_key(view, ord("F"), (w.VK_CONTROL,))
        time.sleep(0.4)
        w.set_text(edit, name)
        time.sleep(1.0)
        w.post_key(edit, w.VK_RETURN)
        new: list = []

        def grab() -> bool:
            for h in _kakao_tops(pid):
                if h not in before and h not in new and w.visible(h) and w.title(h):
                    new.append(h)
            return bool(new)
        _wait(grab, timeout, 0.15)
        w.set_text(edit, "")
        for h in new:
            if w.title(h) == name and w.first_descendant(h, COMPOSER):
                steps.append(f"{label} search opened it")
                return {"opened": name, "already_open": False, "steps": steps}
        for h in new:
            closed.append(w.title(h))
            w.user32.PostMessageW(h, w.WM_CLOSE, 0, 0)
        steps.append(f"{label} search: " + (f"opened {[w.title(h) for h in new]} instead, closed" if new else "nothing opened"))
    raise KakaoError(f"could not open a room titled exactly {name!r} ({'; '.join(steps)}). Rooms opened by "
                     f"mistake were closed again: {closed}. Use the name the chat list shows, or open the room "
                     "once by hand.")


# -- capture -----------------------------------------------------------------
def capture(room, path: str) -> str | None:
    """PrintWindow capture, or None when the session is not drawing (RDP minimized or disconnected)."""
    from PIL import Image
    w.capture_png(room, path)
    with Image.open(path) as im:
        if not im.convert("L").getbbox():
            os.remove(path)
            return None
    return path
