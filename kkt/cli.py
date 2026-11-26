"""kkt - KakaoTalk for Mac over Accessibility. No keystrokes, no focus stealing."""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

from . import bot, wrapped
from .ax import Chat, KakaoError, list_windows
from .capture import snap
from .watch import Watcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "winid_cache.json")


def safe(name: str) -> str:
    return re.sub(r"[^\w가-힣ㄱ-ㅎㅏ-ㅣ-]+", "_", name)


def cmd_rooms(a):
    for w in list_windows():
        print(f"{w['id']}\t{'on ' if w['onscreen'] else 'off'}\t{w['title']}")


def cmd_read(a):
    for m in Chat(a.room, CACHE).messages()[-a.n:]:
        print(f"{'<' if m.dir == 'in' else '>'} {m.text}")


def cmd_send(a):
    Chat(a.room, CACHE).send(a.text)
    print("sent")


def cmd_snap(a):
    chat = Chat(a.room, CACHE)
    wid = chat.cg_window_id()
    if wid is None:
        raise KakaoError(f"chat window {a.room!r} is not open")
    out = a.out or os.path.join(DATA, "caps", f"{safe(a.room)}-{time.strftime('%H%M%S')}.png")
    print(snap(wid, out, None if a.full else (0.45, 0.86)))


def cmd_watch(a):
    """Tab-separated event lines, one per burst - made for Claude Code's Monitor tool."""
    chat = Chat(a.room, CACHE)
    w = Watcher(chat, quiet=a.quiet, archive=os.path.join(DATA, "archive", f"{safe(a.room)}.jsonl"),
                stop_file=os.path.join(DATA, "STOP"))
    for ev in w.events():
        if ev.type != "burst":
            print(ev.type.upper(), ev.detail, sep="\t", flush=True)
            if ev.type == "stop":
                return
            continue
        items = [f"IN: {m.text}" for m in ev.incoming] + [f"REACT: {m.text[:60]}" for m in ev.reactions]
        if len(items) > 30:
            items = items[:5] + [f"... (+{len(items) - 10} more)"] + items[-5:]
        wid = chat.cg_window_id()
        cap = ""
        if wid is not None:
            try:
                cap = snap(wid, os.path.join(DATA, "caps", f"{safe(a.room)}-{time.strftime('%H%M%S')}.png"))
            except Exception as e:
                cap = f"capture-failed: {e}"
        line = " ⏎ ".join(" / ".join(i.splitlines()) for i in items)
        print("BURST", line, cap, sep="\t", flush=True)


def cmd_bot(a):
    bot.run(a.room, a.persona, DATA, model=a.model, min_interval=a.min_interval,
            max_per_hour=a.max_per_hour, quiet=a.quiet, dry_run=a.dry_run)


def cmd_wrapped(a):
    src = os.path.join(DATA, "archive", f"{safe(a.room)}.jsonl")
    if not os.path.exists(src):
        raise KakaoError(f"no archive yet for {a.room!r} - run `kkt watch` or `kkt bot` first")
    out = a.out or os.path.join(DATA, f"wrapped-{safe(a.room)}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(wrapped.render(a.room, wrapped.load(src)))
    print(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="kkt", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rooms", help="list open KakaoTalk windows").set_defaults(fn=cmd_rooms)
    s = sub.add_parser("read", help="print recent messages of an open chat window")
    s.add_argument("room"); s.add_argument("-n", type=int, default=20); s.set_defaults(fn=cmd_read)
    s = sub.add_parser("send", help="send a message to an open chat window")
    s.add_argument("room"); s.add_argument("text"); s.set_defaults(fn=cmd_send)
    s = sub.add_parser("snap", help="capture a chat window in the background")
    s.add_argument("room"); s.add_argument("-o", "--out"); s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_snap)
    s = sub.add_parser("watch", help="print one line per burst of new activity")
    s.add_argument("room"); s.add_argument("--quiet", type=float, default=4.0); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("bot", help="persona bot that replies through a tool-less claude -p")
    s.add_argument("room"); s.add_argument("--persona", default=os.path.join(ROOT, "personas", "example.md"))
    s.add_argument("--model", default="claude-haiku-5-5")
    s.add_argument("--min-interval", type=float, default=15)
    s.add_argument("--max-per-hour", type=int, default=30)
    s.add_argument("--quiet", type=float, default=4.0)
    s.add_argument("--dry-run", action="store_true", help="generate replies but do not send")
    s.set_defaults(fn=cmd_bot)
    s = sub.add_parser("wrapped", help="render the '우정 Wrapped' HTML from the archive")
    s.add_argument("room"); s.add_argument("-o", "--out"); s.set_defaults(fn=cmd_wrapped)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KakaoError as e:
        sys.exit(f"kkt: {e}")
    except KeyboardInterrupt:
        pass
