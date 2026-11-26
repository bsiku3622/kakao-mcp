"""Accessibility access to KakaoTalk for Mac chat windows.

Everything here uses AX attribute reads, AXValue writes and AXPress. There are no
synthetic keyboard/mouse events and no frontmost flips, so the user's foreground
app (and its IME composition) is never touched.

KakaoTalk leaves AXWindows empty, so a chat window is located through
AXFocusedWindow/AXMainWindow first and then through the private
_AXUIElementCreateWithRemoteToken (the alt-tab technique), which finds it no
matter which KakaoTalk window is active. The element id is cached.
"""
from __future__ import annotations

import json
import os
import struct
import time
import unicodedata
from dataclasses import dataclass

import AppKit
import ApplicationServices as AS
import objc
import Quartz
from Foundation import NSBundle, NSData

BUNDLE_ID = "com.kakao.KakaoTalkMac"
_HISERVICES = "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework"
_fns: dict = {}
objc.loadBundleFunctions(NSBundle.bundleWithPath_(_HISERVICES), _fns,
                         [("_AXUIElementCreateWithRemoteToken", b"^{__AXUIElement=}@")])
_remote_token = _fns.get("_AXUIElementCreateWithRemoteToken")

# Bubble geometry (points from the window edge): incoming bubbles hug the left
# (~55-60pt), outgoing ones the right (~18pt). Centered rows are separators.
_IN_MAX_LEFT = 75
_OUT_MAX_RIGHT = 30


class KakaoError(RuntimeError):
    pass


def nfc(s: str | None) -> str:
    return unicodedata.normalize("NFC", s or "")


def attr(e, name):
    if e is None:
        return None
    err, v = AS.AXUIElementCopyAttributeValue(e, name, None)
    return v if err == 0 else None


def _unpack(v, kind):
    if v is None:
        return None
    ok, p = AS.AXValueGetValue(v, kind, None)
    return p if ok else None


def frame(e):
    p = _unpack(attr(e, "AXPosition"), AS.kAXValueCGPointType)
    s = _unpack(attr(e, "AXSize"), AS.kAXValueCGSizeType)
    return (p.x, p.y, s.width, s.height) if p and s else None


def kakao_pid() -> int | None:
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    return apps[0].processIdentifier() if apps else None


def screen_locked() -> bool:
    """While the screen is locked, other apps' AX attributes all read as None."""
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked"))


def list_windows(pid: int | None = None) -> list[dict]:
    """Open KakaoTalk windows from the window server: [{id, title, onscreen}]."""
    pid = pid or kakao_pid()
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0 and w.get("kCGWindowName"):
            out.append({"id": int(w["kCGWindowNumber"]), "title": nfc(w["kCGWindowName"]),
                        "onscreen": bool(w.get("kCGWindowIsOnscreen"))})
    return out


@dataclass
class Message:
    dir: str      # "in" | "out"
    kind: str     # "text" | "media"
    text: str
    decor: tuple  # (row height, unlabeled button count) - changes when reactions appear

    @property
    def key(self):
        return (self.dir, self.text)


def _walk(e, fn):
    if fn(e) is False:
        return
    for c in attr(e, "AXChildren") or []:
        _walk(c, fn)


def _first(e, pred):
    found = []

    def visit(x):
        if found:
            return False
        if pred(x):
            found.append(x)
            return False
    _walk(e, visit)
    return found[0] if found else None


class Chat:
    """One KakaoTalk chat window, addressed by its title (the room name)."""

    def __init__(self, title: str, cache_path: str | None = None, pid: int | None = None):
        self.title = nfc(title)
        self.pid = pid or kakao_pid()
        if self.pid is None:
            raise KakaoError("KakaoTalk is not running")
        self.cache_path = cache_path

    # -- window lookup -------------------------------------------------------
    def _load_cache(self) -> dict:
        if not self.cache_path:
            return {}
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _by_token(self, eid: int):
        if _remote_token is None:
            return None
        token = NSData.dataWithBytes_length_(struct.pack("<iiiQ", self.pid, 0, 0x636F636F, eid), 20)
        return _remote_token(token)

    def _is_me(self, w) -> bool:
        return w is not None and attr(w, "AXRole") == "AXWindow" and nfc(attr(w, "AXTitle")) == self.title

    def window(self, scan_limit: int = 3000):
        app = AS.AXUIElementCreateApplication(self.pid)
        cands = list(attr(app, "AXWindows") or [])
        cands += [w for w in (attr(app, "AXFocusedWindow"), attr(app, "AXMainWindow")) if w is not None]
        for w in cands:
            if self._is_me(w):
                return w
        cache = self._load_cache()
        key = f"{self.pid}:{self.title}"
        if key in cache and self._is_me(w := self._by_token(cache[key])):
            return w
        for eid in range(scan_limit):
            w = self._by_token(eid)
            if self._is_me(w):
                if self.cache_path:
                    cache[key] = eid
                    os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                    with open(self.cache_path, "w") as f:
                        json.dump(cache, f)
                return w
        return None

    def require_window(self):
        if screen_locked():
            raise KakaoError("screen is locked; AX is unavailable until it is unlocked")
        w = self.window()
        if w is None:
            raise KakaoError(f"chat window {self.title!r} is not open")
        return w

    def cg_window_id(self) -> int | None:
        for w in list_windows(self.pid):
            if w["title"] == self.title:
                return w["id"]
        return None

    # -- reading -------------------------------------------------------------
    def row_count(self, win=None) -> int:
        """One cheap AX call - the watcher's change signal between full scans."""
        win = win or self.require_window()
        table = _first(win, lambda e: attr(e, "AXRole") == "AXTable")
        return len(attr(table, "AXRows") or []) if table is not None else -1

    def messages(self, win=None, last_rows: int = 40) -> list[Message]:
        """Messages from the last `last_rows` table rows.

        Walking every row is expensive: KakaoTalk answers AX on its main thread, and
        polling 150+ rows every 1.5s pinned it at ~75% CPU and made each call take
        minutes. The tail is all a watcher needs.
        """
        win = win or self.require_window()
        wf = frame(win)
        table = _first(win, lambda e: attr(e, "AXRole") == "AXTable")
        if not wf or table is None:
            raise KakaoError("chat table not found")
        out: list[Message] = []
        for row in (attr(table, "AXRows") or [])[-last_rows:]:
            rf = frame(row)
            texts, images, buttons = [], [], 0

            def visit(e):
                nonlocal buttons
                role = attr(e, "AXRole")
                if role == "AXTextArea":
                    f = frame(e)
                    if f:
                        texts.append((nfc(attr(e, "AXValue")), f))
                    return False
                if role == "AXImage":
                    f = frame(e)
                    if f and f[2] >= 60:
                        images.append(f)
                if role == "AXButton" and not (attr(e, "AXDescription") or attr(e, "AXTitle")):
                    buttons += 1
            _walk(row, visit)
            items = [("text", t, f) for t, f in texts]
            if not texts and images:   # sticker / photo row: only a big image, no text
                items = [("media", "[이모티콘/사진]", max(images, key=lambda f: f[2] * f[3]))]
            for kind, text, f in items:
                left = f[0] - wf[0]
                right = wf[0] + wf[2] - (f[0] + f[2])
                d = "in" if left < _IN_MAX_LEFT else "out" if right < _OUT_MAX_RIGHT else None
                if d:
                    out.append(Message(d, kind, text, (round(rf[3]) if rf else 0, buttons)))
        return out

    # -- sending -------------------------------------------------------------
    def send(self, text: str, verify_timeout: float = 5.0) -> None:
        """Type into the input field via AXValue and AXPress the send button."""
        text = text.strip()
        if not text:
            raise KakaoError("empty message")
        win = self.require_window()
        field = _first(win, lambda e: attr(e, "AXRole") == "AXTextArea"
                       and attr(e, "AXDescription") == "메시지 입력")
        if field is None:
            raise KakaoError("message input field not found")
        draft = attr(field, "AXValue") or ""
        if draft.strip():
            raise KakaoError(f"input field already has a draft, not overwriting: {draft[:60]!r}")
        if AS.AXUIElementSetAttributeValue(field, "AXValue", text) != 0:
            raise KakaoError("could not set the input field value")
        button = None
        for _ in range(15):
            time.sleep(0.2)
            b = _first(win, lambda e: attr(e, "AXRole") == "AXButton" and attr(e, "AXTitle") == "전송")
            if b is not None and attr(b, "AXEnabled"):
                button = b
                break
        if button is None:
            raise KakaoError("send button did not enable; text left in the input field")
        # A busy KakaoTalk can answer AXPress with a timeout error and still perform
        # it, so the error alone is not trusted: the chat itself is checked.
        press_err = AS.AXUIElementPerformAction(button, "AXPress")
        deadline = time.time() + verify_timeout
        while time.time() < deadline:
            time.sleep(0.4)
            outs = [m.text for m in self.messages(win, last_rows=10) if m.dir == "out"]
            if outs and outs[-1].strip() == text:
                return
        raise KakaoError(f"message did not show up as the last outgoing bubble (AXPress err={press_err}); "
                         "check the input field")
