"""kkt MCP server - KakaoTalk for Mac as deterministic MCP tools.

Every action is an AX attribute read/write or an element-scoped AX action on the
official app. No keystrokes, no mouse events, no network traffic of its own.
A send is judged by the app itself - a new outgoing bubble with the same text,
or else the composer the app cleared when it sent - and the window capture that
comes back is for a visual second look, not the verdict.

Transports:
  (default)   stdio - this process drives KakaoTalk, so it needs the caller's
              Accessibility and Screen Recording permissions.
  --http      a daemon on 127.0.0.1:KKT_HTTP_PORT (default 8799) behind a bearer
              token in data/http-token. Run it from the Kakao MCP launcher app so
              the permissions belong to that app (see launcher/).
  --bridge    a stdio relay to the daemon. It never touches KakaoTalk, so it works
              from any context, including `ssh macbook kakao-mcp --bridge`.

Optional switches:
  KKT_ALLOWED_ROOMS     comma-separated window titles that may be sent to (unset = any room)
  data/STOP             while this file exists, sends are refused
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import threading
import time

import ApplicationServices as AS

from .ax import Chat, KakaoError, kakao_pid, list_windows, nfc, screen_locked
from . import transport
from .capture import snap
from .deliver import composer, deliver
from .open_chat import MAIN_TITLE, hid_idle_seconds, open_chat

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "winid_cache.json")
LOG = os.path.join(DATA, "mcp-log.jsonl")
STOP = os.path.join(DATA, "STOP")
LOCK_FILE = os.path.join(DATA, ".kakao.lock")
HTTP_PORT = int(os.environ.get("KKT_HTTP_PORT", "8799"))
ALLOWED = {nfc(r.strip()) for r in os.environ.get("KKT_ALLOWED_ROOMS", "").split(",") if r.strip()}

NOT_A_ROOM_TTL = 600.0               # remember windows without a composer (e.g. "Window") this long

_thread_lock = threading.Lock()
_not_a_room: dict[tuple[int, str], float] = {}


@contextlib.contextmanager
def _lock():
    """One KakaoTalk operation at a time - across threads and across processes
    (a stdio server and the daemon must not type into the same composer at once)."""
    os.makedirs(DATA, exist_ok=True)
    with _thread_lock, open(LOCK_FILE, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


mcp = MCPServer(
    "kakaotalk",
    instructions=(
        "KakaoTalk for Mac through Accessibility. Rooms are addressed by their chat window title, "
        "which is the name shown in the chat list without a '팀채팅, ' / '오픈채팅, ' / '나와의 채팅, ' prefix. "
        "send_message opens a room that has no window yet by itself, verifies delivery, and returns "
        "status sent / not_sent / uncertain / refused plus a capture of the chat. Never resend after "
        "'sent' or 'uncertain'. Reading walks KakaoTalk's UI on its main thread, so read only what is "
        "needed and do not poll."
    ),
)


def _tool_errors(fn):
    """KakaoError -> ToolError, so the model sees why a call was refused."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KakaoError as e:
            raise ToolError(str(e)) from e
    return wrapper


def _log(**kw):
    os.makedirs(DATA, exist_ok=True)
    kw["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def _rooms() -> list[dict]:
    """Chat windows only: a KakaoTalk window counts as a room when it has a composer."""
    pid = kakao_pid()
    out, now = [], time.time()
    for w in list_windows(pid):
        key = (pid, w["title"])
        if w["title"] == MAIN_TITLE or now - _not_a_room.get(key, -1e9) < NOT_A_ROOM_TTL:
            continue
        win = Chat(w["title"], CACHE, pid).window()
        if win is None or composer(win) is None:
            _not_a_room[key] = now
            continue
        out.append({"title": w["title"], "on_screen": w["onscreen"]})
    return out


def _capture(chat: Chat, full: bool) -> Image | None:
    wid = chat.cg_window_id()
    if wid is None:
        return None
    safe = "".join(ch if ch.isalnum() else "_" for ch in chat.title)
    path = os.path.join(DATA, "caps", f"mcp-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png")
    snap(wid, path, None if full else (0.45, 0.86))
    with open(path, "rb") as f:
        return Image(data=f.read(), format="png")


def _guard(room: str, text: str) -> None:
    if os.path.exists(STOP):
        raise KakaoError(f"sending is stopped while {STOP} exists")
    if ALLOWED and room not in ALLOWED:
        raise KakaoError(f"{room!r} is not in KKT_ALLOWED_ROOMS {sorted(ALLOWED)}")
    if not text:
        raise KakaoError("empty message")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def kakao_status() -> dict:
    """KakaoTalk state: running, permissions, screen lock, open rooms."""
    pid = kakao_pid()
    main = next((w for w in list_windows(pid) if w["title"] == MAIN_TITLE), None) if pid else None
    titles = [w["title"] for w in list_windows(pid)] if pid else []
    return {
        "platform": "macos",
        "running": pid is not None,
        # Signed out, KakaoTalk keeps only a window titled 로그인 and no 카카오톡 main window.
        "logged_in": MAIN_TITLE in titles and "로그인" not in titles,
        "accessibility_trusted": bool(AS.AXIsProcessTrusted()),
        # Without it window titles read empty, so no room is listed. Re-read at process start.
        "screen_recording": bool(__import__("Quartz").CGPreflightScreenCaptureAccess()),
        "screen_locked": screen_locked(),
        "main_window_on_screen": bool(main and main["onscreen"]),
        "open_rooms": _rooms() if pid else [],
        "user_idle_seconds": hid_idle_seconds(),
        "allowed_rooms": sorted(ALLOWED) or "any",
        "stopped": os.path.exists(STOP),
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def list_rooms() -> list[dict]:
    """Chat rooms open in their own window, on any desktop. send_message also reaches rooms that
    are not listed here (it opens them); on_screen=false only means another desktop or minimized."""
    if kakao_pid() is None:
        raise KakaoError("KakaoTalk is not running")
    with _lock():
        return _rooms()


def _open_room_or_raise(room: str) -> Chat:
    chat = Chat(room, CACHE)
    if chat.window() is None:
        open_chat(room, CACHE)
        chat = Chat(room, CACHE)
    return chat


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def read_messages(room: str, limit: int = 20, full: bool = True) -> dict:
    """Recent messages of a room, oldest first (opens the room if it has no window).
    dir: in (received) / out (sent by me). kind=media is a sticker or photo; reactions are
    not exposed - use capture_room to see them. Long messages that KakaoTalk folds behind
    전체보기 are read in full (each opens and closes a small viewer window for a moment);
    full=false returns the folded text, which ends with "…"."""
    room = nfc(room)
    limit = max(1, min(int(limit), 60))
    with _lock():
        chat = _open_room_or_raise(room)
        msgs = chat.messages(last_rows=limit + 5)[-limit:]
        texts = [chat.full_text(m) if full and m.folded else m.text for m in msgs]
    return {"room": room, "messages": [{"dir": m.dir, "kind": m.kind, "text": t} for m, t in zip(msgs, texts)]}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def capture_room(room: str, full: bool = False) -> list:
    """Background capture of a chat window (no activation; opens the room if it has no window).
    full=false keeps the band around the latest messages."""
    room = nfc(room)
    with _lock():
        chat = _open_room_or_raise(room)
        img = _capture(chat, full)
    if img is None:
        raise KakaoError(f"window server has no window titled {room!r}")
    return [f"capture of {room!r}", img]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
@_tool_errors
def open_room(room: str) -> dict:
    """Open a chat in its own window (search + context menu 채팅방 열기, AX only). If KakaoTalk is on
    another desktop it is brought forward for about a second, after the keyboard and mouse rest,
    and the previous app is handed back. send_message does this by itself; call this only to
    open a room ahead of time. Opening marks the room as read."""
    with _lock():
        return open_chat(nfc(room), CACHE)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=False, open_world_hint=True),
          structured_output=False)
def send_message(room: str, text: str, open_if_needed: bool = True, allow_repeat: bool = False,
                 capture: bool = True) -> list:
    """Send a text message to a room addressed by its chat window title (see the server
    instructions for the name rule). A room without a window is opened first.

    Returns a JSON status line - sent / not_sent / uncertain / refused - and, by default, a
    capture of the chat. Refused when the room's newest outgoing bubble already shows this text
    (a repeat across tools); allow_repeat=true sends it anyway. Never resend after 'sent' or
    'uncertain'."""
    room, text = nfc(room), nfc(text).strip()
    with _lock():
        result: dict = {"room": room, "chars": len(text)}
        try:
            _guard(room, text)
            chat = Chat(room, CACHE)
            if chat.window() is None:
                if not open_if_needed:
                    raise KakaoError(f"no chat window titled exactly {room!r} is open (open_if_needed=false)")
                result["opened"] = open_chat(room, CACHE)
                chat = Chat(room, CACHE)
            result.update(deliver(chat, text, allow_repeat=allow_repeat))
        except KakaoError as e:
            result.update(status="refused", detail=str(e))
        _log(tool="send_message", room=room, text=text[:200], status=result["status"],
             verified_by=result.get("verified_by"), detail=result.get("detail"))
        out: list = [json.dumps(result, ensure_ascii=False)]
        if capture and result["status"] != "refused":
            try:
                img = _capture(Chat(room, CACHE), False)
                if img is not None:
                    out.append(img)
            except Exception as e:  # the verdict above stands without a picture
                out.append(f"capture failed: {e}")
    return out


def _ask_permissions() -> None:
    """Ask once, so the launcher app shows up in System Settings > Privacy & Security."""
    import Quartz
    AS.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    Quartz.CGRequestScreenCaptureAccess()


def main():
    transport.main(mcp, DATA, HTTP_PORT, prepare_daemon=_ask_permissions)


if __name__ == "__main__":
    main()
