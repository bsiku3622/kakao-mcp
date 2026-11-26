"""kkt MCP server - KakaoTalk for Mac as deterministic MCP tools (stdio).

Every action is an AX attribute read/write or an element-scoped AX action on the
official app. No keystrokes, no mouse events, no network traffic of its own.
A send is judged by the app itself (a new outgoing bubble with the same text in
rows that did not exist before the press); the window capture that comes back
is for a visual second look, not the verdict.

Transports:
  (default)   stdio - this process drives KakaoTalk, so it needs the caller's
              Accessibility and Screen Recording permissions.
  --http      a daemon on 127.0.0.1:KKT_HTTP_PORT (default 8799) behind a bearer
              token in data/http-token. Run it from the Kakao MCP launcher app so
              the permissions belong to that app (see launcher/).
  --bridge    a stdio relay to the daemon. It never touches KakaoTalk, so it works
              from any context, including `ssh macbook kakao-mcp --bridge`.

Guardrails (environment variables):
  KKT_ALLOWED_ROOMS     comma-separated window titles that may be sent to (empty = any open room)
  KKT_MAX_PER_HOUR      sends per rolling hour (default 30)
  KKT_MIN_INTERVAL      seconds between sends (default 2)
  KKT_REPEAT_WINDOW     seconds in which the same text to the same room is refused (default 120)
  data/STOP             while this file exists, sends are refused
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hmac
import json
import os
import secrets
import sys
import threading
import time
from collections import deque

import ApplicationServices as AS

from .ax import Chat, KakaoError, attr, kakao_pid, list_windows, nfc, screen_locked, _first
from .capture import snap
from .open_chat import MAIN_TITLE, hid_idle_seconds, open_chat

import functools

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "winid_cache.json")
LOG = os.path.join(DATA, "mcp-log.jsonl")
STOP = os.path.join(DATA, "STOP")
TOKEN_FILE = os.path.join(DATA, "http-token")
LOCK_FILE = os.path.join(DATA, ".kakao.lock")
HTTP_PORT = int(os.environ.get("KKT_HTTP_PORT", "8799"))
MAX_TEXT = 1000

ALLOWED = {nfc(r.strip()) for r in os.environ.get("KKT_ALLOWED_ROOMS", "").split(",") if r.strip()}
MAX_PER_HOUR = int(os.environ.get("KKT_MAX_PER_HOUR", "30"))
MIN_INTERVAL = float(os.environ.get("KKT_MIN_INTERVAL", "2"))
REPEAT_WINDOW = float(os.environ.get("KKT_REPEAT_WINDOW", "120"))

_thread_lock = threading.Lock()
_sent: deque[tuple[float, str, str]] = deque()  # (time, room, text) of verified or uncertain sends


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
        "KakaoTalk for Mac through Accessibility. Rooms are addressed by their chat window title "
        "(see list_rooms). send_message verifies delivery itself and returns status sent / not_sent / "
        "uncertain plus a capture of the chat for a visual check. Never resend after 'uncertain': the "
        "message may have gone out; look at the capture or read_messages instead. Reading walks KakaoTalk's "
        "UI on its main thread, so read only what is needed and do not poll."
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
    return [{"title": w["title"], "on_screen": w["onscreen"]}
            for w in list_windows() if w["title"] != MAIN_TITLE]


def _not_open(room: str) -> KakaoError:
    titles = [r["title"] for r in _rooms()]
    return KakaoError(f"no chat window titled exactly {room!r} is open. Open rooms: {titles}. "
                      "Use open_room (or open_if_needed=true) to open it.")


def _capture(chat: Chat, full: bool) -> Image | None:
    wid = chat.cg_window_id()
    if wid is None:
        return None
    safe = "".join(ch if ch.isalnum() else "_" for ch in chat.title)
    path = os.path.join(DATA, "caps", f"mcp-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png")
    snap(wid, path, None if full else (0.45, 0.86))
    with open(path, "rb") as f:
        return Image(data=f.read(), format="png")


def _guard(room: str, text: str, allow_repeat: bool) -> None:
    if os.path.exists(STOP):
        raise KakaoError(f"sending is stopped while {STOP} exists")
    if ALLOWED and room not in ALLOWED:
        raise KakaoError(f"{room!r} is not in KKT_ALLOWED_ROOMS {sorted(ALLOWED)}")
    if not text:
        raise KakaoError("empty message")
    if len(text) > MAX_TEXT:
        raise KakaoError(f"message is {len(text)} characters; the limit is {MAX_TEXT} "
                         "(long bubbles fold behind 전체보기 and cannot be verified)")
    now = time.time()
    while _sent and now - _sent[0][0] > 3600:
        _sent.popleft()
    if len(_sent) >= MAX_PER_HOUR:
        raise KakaoError(f"hourly cap reached ({MAX_PER_HOUR} sends in the last hour)")
    if _sent and now - _sent[-1][0] < MIN_INTERVAL:
        raise KakaoError(f"sends must be at least {MIN_INTERVAL:g}s apart")
    if not allow_repeat and any(r == room and t == text and now - ts < REPEAT_WINDOW for ts, r, t in _sent):
        raise KakaoError(f"the same text went to {room!r} less than {REPEAT_WINDOW:g}s ago; "
                         "pass allow_repeat=true if this repeat is intended")


def deliver(chat: Chat, text: str, verify_timeout: float = 6.0) -> dict:
    """AXValue into the composer, AXPress 전송, then judge by the chat itself.

    Verified only by an outgoing bubble with this exact text among rows that did
    not exist before the press, so an identical earlier message cannot pass.
    """
    win = chat.require_window()
    field = _first(win, lambda e: attr(e, "AXRole") == "AXTextArea" and attr(e, "AXDescription") == "메시지 입력")
    table = _first(win, lambda e: attr(e, "AXRole") == "AXTable")
    if field is None or table is None:
        raise KakaoError("composer or chat table not found in the chat window")
    draft = attr(field, "AXValue") or ""
    if draft.strip():
        raise KakaoError(f"the composer already holds a draft; not overwriting it ({len(draft)} chars)")
    before = len(attr(table, "AXRows") or [])
    if AS.AXUIElementSetAttributeValue(field, "AXValue", text) != 0:
        raise KakaoError("could not set the composer text")
    button = None
    for _ in range(15):
        time.sleep(0.2)
        b = _first(win, lambda e: attr(e, "AXRole") == "AXButton" and attr(e, "AXTitle") == "전송")
        if b is not None and attr(b, "AXEnabled"):
            button = b
            break
    if button is None:
        AS.AXUIElementSetAttributeValue(field, "AXValue", "")
        return {"status": "not_sent", "detail": "전송 button never enabled; composer cleared"}
    press_err = AS.AXUIElementPerformAction(button, "AXPress")  # a busy app may time out and still send
    t0 = time.time()
    while time.time() - t0 < verify_timeout:
        time.sleep(0.4)
        now_rows = len(attr(table, "AXRows") or [])
        if now_rows > before:
            new = chat.messages(win, last_rows=now_rows - before)
            if any(m.dir == "out" and m.text.strip() == text for m in new):
                return {"status": "sent", "detail": f"verified in {time.time() - t0:.1f}s "
                        f"({now_rows - before} new rows, AXPress err={press_err})"}
    left = (attr(field, "AXValue") or "").strip()
    if left == text:
        AS.AXUIElementSetAttributeValue(field, "AXValue", "")
        return {"status": "not_sent", "detail": f"text was still in the composer (AXPress err={press_err}); cleared"}
    return {"status": "uncertain", "detail": f"composer is empty but no matching new bubble within "
            f"{verify_timeout:g}s (AXPress err={press_err}); do not resend, check the capture"}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def kakao_status() -> dict:
    """KakaoTalk state: running, permissions, screen lock, open rooms, guardrail settings."""
    pid = kakao_pid()
    main = next((w for w in list_windows(pid) if w["title"] == MAIN_TITLE), None) if pid else None
    return {
        "running": pid is not None,
        "accessibility_trusted": bool(AS.AXIsProcessTrusted()),
        # Without it window titles read empty, so no room is listed. Re-read at process start.
        "screen_recording": bool(__import__("Quartz").CGPreflightScreenCaptureAccess()),
        "screen_locked": screen_locked(),
        "main_window_on_screen": bool(main and main["onscreen"]),
        "open_rooms": _rooms() if pid else [],
        "user_idle_seconds": hid_idle_seconds(),
        "guardrails": {"allowed_rooms": sorted(ALLOWED) or "any open room", "max_per_hour": MAX_PER_HOUR,
                       "min_interval_s": MIN_INTERVAL, "repeat_window_s": REPEAT_WINDOW,
                       "stopped": os.path.exists(STOP), "max_chars": MAX_TEXT},
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def list_rooms() -> list[dict]:
    """Chat rooms that are open in their own window. Only these can be read or sent to
    without opening (open_room). on_screen=false means another desktop or minimized - still usable."""
    if kakao_pid() is None:
        raise KakaoError("KakaoTalk is not running")
    return _rooms()


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def read_messages(room: str, limit: int = 20) -> dict:
    """Recent messages of an open room, oldest first. dir: in (received) / out (sent by me).
    kind=media is a sticker or photo; reactions are not exposed - use capture_room to see them."""
    room = nfc(room)
    limit = max(1, min(int(limit), 60))
    with _lock():
        chat = Chat(room, CACHE)
        if chat.window() is None:
            raise _not_open(room)
        msgs = chat.messages(last_rows=limit + 5)[-limit:]
    return {"room": room, "messages": [{"dir": m.dir, "kind": m.kind, "text": m.text} for m in msgs]}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def capture_room(room: str, full: bool = False) -> list:
    """Background capture of an open chat window (no activation). full=false keeps the band
    around the latest messages."""
    room = nfc(room)
    with _lock():
        chat = Chat(room, CACHE)
        if chat.window() is None:
            raise _not_open(room)
        img = _capture(chat, full)
    if img is None:
        raise KakaoError(f"window server has no window titled {room!r}")
    return [f"capture of {room!r}", img]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
@_tool_errors
def open_room(room: str) -> dict:
    """Open a chat in its own window from KakaoTalk's chat list (search + context menu 채팅방 열기,
    AX only). Needs the main window on the desktop being shown and the user's hands off the
    keyboard and mouse for 2s; otherwise it refuses and says why. Opening marks the room as read."""
    with _lock():
        return open_chat(nfc(room), CACHE)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=False, open_world_hint=True),
          structured_output=False)
def send_message(room: str, text: str, open_if_needed: bool = False, allow_repeat: bool = False,
                 capture: bool = True) -> list:
    """Send a text message to a room addressed by its exact chat window title.

    Returns a JSON status line - sent / not_sent / uncertain / refused - and, by default, a
    capture of the chat. Never resend after 'uncertain'."""
    room, text = nfc(room), nfc(text).strip()
    with _lock():
        result: dict = {"room": room, "chars": len(text)}
        try:
            _guard(room, text, allow_repeat)
            chat = Chat(room, CACHE)
            if chat.window() is None:
                if not open_if_needed:
                    raise _not_open(room)
                result["opened"] = open_chat(room, CACHE)
            result.update(deliver(chat, text))
        except KakaoError as e:
            result.update(status="refused", detail=str(e))
        if result["status"] in ("sent", "uncertain"):
            _sent.append((time.time(), room, text))
        _log(tool="send_message", room=room, text=text[:200], status=result["status"], detail=result.get("detail"))
        out: list = [json.dumps(result, ensure_ascii=False)]
        if capture and result["status"] != "refused":
            try:
                img = _capture(Chat(room, CACHE), False)
                if img is not None:
                    out.append(img)
            except Exception as e:  # the verdict above stands without a picture
                out.append(f"capture failed: {e}")
    return out


def _token(create: bool) -> str:
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    if not create:
        sys.exit(f"kakao-mcp: no daemon token at {TOKEN_FILE}; start the daemon (Kakao MCP launcher) first")
    os.makedirs(DATA, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


class _BearerGate:
    """ASGI guard: every HTTP request must carry the daemon's bearer token."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def run_daemon() -> None:
    import uvicorn
    import Quartz
    # Ask once, so the launcher app shows up in System Settings > Privacy & Security.
    AS.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    Quartz.CGRequestScreenCaptureAccess()
    app = _BearerGate(mcp.streamable_http_app(host="127.0.0.1"), _token(create=True))
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="warning")


def run_bridge() -> None:
    """Relay stdio <-> the local daemon, message for message."""
    import logging

    import anyio
    for name in ("httpx", "httpx2", "httpcore", "mcp.client"):   # INFO lines per request otherwise
        logging.getLogger(name).setLevel(logging.WARNING)
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    from mcp.server.stdio import stdio_server

    async def relay():
        headers = {"Authorization": f"Bearer {_token(create=False)}"}
        async with create_mcp_http_client(headers=headers) as http, \
                streamable_http_client(f"http://127.0.0.1:{HTTP_PORT}/mcp", http_client=http) as (r_up, w_up), \
                stdio_server() as (r_in, w_in):
            async with anyio.create_task_group() as tg:
                async def pump(src, dst, label):
                    async for msg in src:
                        if isinstance(msg, Exception):
                            print(f"kakao-mcp bridge ({label}): {msg}", file=sys.stderr)
                            continue
                        await dst.send(msg)
                    tg.cancel_scope.cancel()
                tg.start_soon(pump, r_in, w_up, "client->daemon")
                tg.start_soon(pump, r_up, w_in, "daemon->client")

    anyio.run(relay)


def main():
    p = argparse.ArgumentParser(prog="kakao-mcp")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--http", action="store_true", help="run the local daemon on 127.0.0.1")
    g.add_argument("--bridge", action="store_true", help="relay stdio to the local daemon")
    a = p.parse_args()
    if a.http:
        run_daemon()
    elif a.bridge:
        run_bridge()
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
