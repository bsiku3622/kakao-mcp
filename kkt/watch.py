"""Poll a chat and turn changes into quiet-terminated bursts.

A burst collects new incoming messages and reaction changes and is released
once nothing new has happened for `quiet` seconds, so a friend who sends ten
one-word lines gets one reply, not ten.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .ax import Chat, KakaoError, Message, screen_locked


@dataclass
class Event:
    type: str                                  # burst | locked | unlocked | lost | back | resync | error
    incoming: list[Message] = field(default_factory=list)
    reactions: list[Message] = field(default_factory=list)   # messages whose decor changed
    detail: str = ""


def appended(prev: list[Message], cur: list[Message], tail: int = 5) -> int | None:
    """How many items cur has after prev's last `tail` items; None if they can't be aligned."""
    if not prev:
        return len(cur)
    m = min(tail, len(prev))
    want = [x.key for x in prev[-m:]]
    keys = [x.key for x in cur]
    for j in range(len(cur) - 1, m - 2, -1):
        if keys[j - m + 1:j + 1] == want:
            return len(cur) - 1 - j
    return None


class Watcher:
    def __init__(self, chat: Chat, quiet: float = 4.0, poll: float = 2.0, archive: str | None = None,
                 stop_file: str | None = None, full_every: float = 30.0, scan_rows: int = 25):
        """Polls only the row count every `poll` seconds; the expensive row walk runs
        when the count changes and every `full_every` seconds (to catch reactions).
        KakaoTalk answers AX on its main thread, and walking 40 rows every 3s kept
        it at ~80% CPU."""
        self.chat, self.quiet, self.poll, self.archive = chat, quiet, poll, archive
        self.stop_file, self.full_every, self.scan_rows = stop_file, full_every, scan_rows
        self.prev: list[Message] | None = None
        self._rows = -1
        self._last_full = 0.0

    def _archive(self, msgs: list[Message], seed: bool = False) -> None:
        if not self.archive or not msgs:
            return
        os.makedirs(os.path.dirname(self.archive), exist_ok=True)
        now = time.time()
        with open(self.archive, "a", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps({"ts": None if seed else now, "seen": now, "dir": m.dir,
                                    "kind": m.kind, "text": m.text}, ensure_ascii=False) + "\n")

    def history(self) -> list[Message]:
        return list(self.prev or [])

    def events(self):
        locked = lost = False
        incoming: list[Message] = []
        reactions: list[Message] = []
        last = 0.0
        while True:
            if self.stop_file and os.path.exists(self.stop_file):
                yield Event("stop", detail=self.stop_file)
                return
            try:
                if screen_locked():
                    if not locked:
                        locked = True
                        yield Event("locked")
                    time.sleep(10)
                    continue
                if locked:
                    locked = False
                    yield Event("unlocked")
                win = self.chat.window()
                if win is None:
                    if not lost:
                        lost = True
                        yield Event("lost")
                    time.sleep(5)
                    continue
                if lost:
                    lost = False
                    yield Event("back")
                rows = self.chat.row_count(win)
                if self.prev is not None and rows == self._rows and time.time() - self._last_full < self.full_every:
                    if (incoming or reactions) and time.time() - last >= self.quiet:
                        yield Event("burst", incoming, reactions)
                        incoming, reactions = [], []
                    time.sleep(self.poll)
                    continue
                self._rows, self._last_full = rows, time.time()
                cur = self.chat.messages(win, last_rows=self.scan_rows)
                if self.prev is None:
                    self.prev = cur
                    if self.archive and not os.path.exists(self.archive):
                        self._archive(cur, seed=True)
                    # messages that arrived while nobody was watching (e.g. screen locked)
                    pending = []
                    for m in reversed(cur):
                        if m.dir == "out":
                            break
                        pending.insert(0, m)
                    if pending:
                        incoming += pending
                        last = 0.0
                    continue
                k = appended(self.prev, cur)
                if k is None:
                    yield Event("resync")
                else:
                    new = cur[len(cur) - k:]
                    self._archive(new)
                    got = [m for m in new if m.dir == "in"]
                    old = cur[:len(cur) - k]
                    n = min(15, len(old), len(self.prev))
                    changed = [b for a, b in zip(self.prev[-n:], old[-n:]) if n
                               and a.key == b.key and a.decor != b.decor]
                    if got or changed:
                        incoming += got
                        reactions += changed
                        last = time.time()
                self.prev = cur
                if (incoming or reactions) and time.time() - last >= self.quiet:
                    yield Event("burst", incoming, reactions)
                    incoming, reactions = [], []
            except KakaoError as e:
                yield Event("error", detail=str(e))
                time.sleep(10)
            time.sleep(self.poll)
