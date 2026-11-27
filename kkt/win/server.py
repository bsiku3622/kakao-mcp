"""kakao-mcp for Windows: the same tools as the macOS server, backed by kkt.win.kakao.

Run it inside the Windows session where KakaoTalk runs (window messages do not cross
sessions): scripts/install-daemon-win.ps1 registers a logon task that starts
`--http`, and clients connect over HTTP or through `--bridge`.

Optional switches: KKT_ALLOWED_ROOMS (comma-separated window titles) and data/STOP.
Your own messages are recognised by sender name: put your KakaoTalk name(s) in
data/me.txt, one per line, or in KKT_ME (comma-separated).
"""
from __future__ import annotations

import functools
import json
import os
import threading
import time

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .. import transport
from . import kakao as K

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data")
LOG = os.path.join(DATA, "mcp-log.jsonl")
STOP = os.path.join(DATA, "STOP")
HTTP_PORT = int(os.environ.get("KKT_HTTP_PORT", "8799"))
ALLOWED = {r.strip() for r in os.environ.get("KKT_ALLOWED_ROOMS", "").split(",") if r.strip()}

_lock = threading.Lock()

mcp = MCPServer(
    "kakaotalk-win",
    instructions=(
        "KakaoTalk for Windows through window messages. Rooms are addressed by their chat window title, "
        "which is the name the chat list shows. send_message opens a room that has no window by itself, "
        "verifies delivery, and returns sent / not_sent / refused. Never resend after 'sent'. Messages "
        "of about 500+ characters are read cut (folded=true). Captures are blank while the Windows "
        "session is not shown (RDP minimized or disconnected)."
    ),
)


def _me() -> set[str]:
    names = {n.strip() for n in os.environ.get("KKT_ME", "").split(",") if n.strip()}
    try:
        with open(os.path.join(DATA, "me.txt"), encoding="utf-8") as f:
            names |= {line.strip() for line in f if line.strip()}
    except OSError:
        pass
    return names


def _tool_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except K.KakaoError as e:
            raise ToolError(str(e)) from e
    return wrapper


def _log(**kw):
    os.makedirs(DATA, exist_ok=True)
    kw["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def _room(name: str, open_if_needed: bool = True):
    h = K.room_window(name)
    if h is None and open_if_needed:
        K.open_room(name)
        h = K.room_window(name)
    if h is None:
        raise K.KakaoError(f"no chat window titled exactly {name!r} is open")
    return h


def _capture(h, name: str) -> Image | None:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)
    os.makedirs(os.path.join(DATA, "caps"), exist_ok=True)
    path = K.capture(h, os.path.join(DATA, "caps", f"mcp-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png"))
    if path is None:
        return None
    with open(path, "rb") as f:
        return Image(data=f.read(), format="png")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def kakao_status() -> dict:
    """KakaoTalk state on this Windows PC: running, signed in, open rooms."""
    pid = K.kakao_pid()
    return {
        "platform": "windows",
        "running": pid is not None,
        "logged_in": K.logged_in(),
        "open_rooms": [{"title": t} for _, t in K.rooms()],
        "me": sorted(_me()) or "unset - put your KakaoTalk name in data/me.txt",
        "allowed_rooms": sorted(ALLOWED) or "any",
        "stopped": os.path.exists(STOP),
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def list_rooms() -> list[dict]:
    """Chat rooms open in their own window. send_message also reaches rooms not listed (it opens them)."""
    with _lock:
        return [{"title": t} for _, t in K.rooms()]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def read_messages(room: str, limit: int = 20, full: bool = True) -> dict:
    """Recent messages, oldest first (opens the room if needed): dir in/out, sender, time, text.
    Messages of about 500+ characters come back cut (folded=true). `full` is accepted for
    parity with the macOS server; Windows cannot expand folded messages yet."""
    limit = max(1, min(int(limit), 200))
    with _lock:
        msgs = K.read(_room(room), _me(), limit)
    return {"room": room, "messages": msgs}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_tool_errors
def capture_room(room: str, full: bool = False) -> list:
    """Capture of a chat window (no activation). Blank while the session is not shown."""
    with _lock:
        h = _room(room)
        img = _capture(h, room)
    if img is None:
        return ["no capture: the Windows session is not drawing (RDP minimized or disconnected); use read_messages"]
    return [f"capture of {room!r}", img]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
@_tool_errors
def open_room(room: str) -> dict:
    """Open a chat in its own window via the friends search, then the chat search. A window that
    opens with another title is closed again. Opening marks the room as read."""
    with _lock:
        return K.open_room(room)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=False, open_world_hint=True),
          structured_output=False)
def send_message(room: str, text: str, open_if_needed: bool = True, allow_repeat: bool = False,
                 capture: bool = True) -> list:
    """Send a text message to a room addressed by its chat window title. Returns a JSON status line -
    sent / not_sent / refused - and a capture when the session is drawing. Refused when the room's
    newest outgoing message already shows this text (allow_repeat=true sends anyway). Never resend
    after 'sent'."""
    text = text.strip()
    with _lock:
        result: dict = {"room": room, "chars": len(text)}
        try:
            if os.path.exists(STOP):
                raise K.KakaoError(f"sending is stopped while {STOP} exists")
            if ALLOWED and room not in ALLOWED:
                raise K.KakaoError(f"{room!r} is not in KKT_ALLOWED_ROOMS {sorted(ALLOWED)}")
            if not text:
                raise K.KakaoError("empty message")
            me = _me()
            if not me:
                raise K.KakaoError("your KakaoTalk name is not set (data/me.txt), so sent messages cannot be told apart")
            h = _room(room, open_if_needed)
            result.update(K.deliver(h, text, me, allow_repeat=allow_repeat))
        except K.KakaoError as e:
            result.update(status="refused", detail=str(e))
        _log(tool="send_message", room=room, text=text[:200], status=result["status"],
             verified_by=result.get("verified_by"), detail=result.get("detail"))
        out: list = [json.dumps(result, ensure_ascii=False)]
        if capture and result["status"] != "refused":
            try:
                img = _capture(K.room_window(room), room)
                if img is not None:
                    out.append(img)
            except Exception as e:
                out.append(f"capture failed: {e}")
    return out


def main():
    transport.main(mcp, DATA, HTTP_PORT)


if __name__ == "__main__":
    main()
