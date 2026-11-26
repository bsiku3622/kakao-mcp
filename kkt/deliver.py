"""Send one message into an open chat window and judge the result from the app.

Shared by the CLI (`kkt send`), the persona bot and the MCP server, so every path
types, presses and verifies the same way.

Measured pitfalls this handles:
- AXPress on 전송 usually returns -25200 (kAXErrorCannotComplete) and still sends.
- Long bubbles fold behind 전체보기, line breaks come back as other separators, and a
  URL adds a link-card row after the text bubble, so exact "last bubble == text"
  comparisons fail. Bubbles are compared after normalising whitespace, by their
  opening characters, anywhere in the rows after the press.
- A window on another desktop refreshes its rows late and may not grow its row
  count, so the verdict counts matching outgoing bubbles in the tail instead of
  diffing row indices, and waits longer.
- KakaoTalk clears the composer only when it sends, so a cleared composer without
  a matched bubble is still reported as sent (verified_by=composer_cleared).
"""
from __future__ import annotations

import re
import time

import ApplicationServices as AS

from .ax import Chat, KakaoError, attr, find, list_windows, nfc

COMPOSER = "메시지 입력"
TAIL_ROWS = 15
VERIFY_ON_SCREEN, VERIFY_OFF_SCREEN = 8.0, 15.0
AFTER_CLEARED = 4.0

_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Text as it renders: NFC, any line break or run of spaces as one space."""
    return _WS.sub(" ", nfc(s).replace(" ", "\n").replace(" ", "\n")).strip()


def same_text(bubble: str, sent_n: str) -> bool:
    """A bubble shows the sent text: equal after normalising, the same opening 40
    characters (folded long bubbles), or a long-enough leading part of it."""
    b = normalize(bubble).rstrip("…").rstrip()
    if not b:
        return False
    if b == sent_n:
        return True
    head = min(40, len(sent_n))
    if len(b) >= head and b[:head] == sent_n[:head]:
        return True
    return len(b) >= 20 and sent_n.startswith(b)


def composer(win):
    return find(win, lambda e: attr(e, "AXRole") == "AXTextArea" and attr(e, "AXDescription") == COMPOSER)


def send_button(win):
    return find(win, lambda e: attr(e, "AXRole") == "AXButton" and attr(e, "AXTitle") == "전송")


def deliver(chat: Chat, text: str, allow_repeat: bool = False) -> dict:
    """AXValue into the composer, AXPress 전송, then judge by the chat itself.

    Returns {"status": sent | not_sent, "verified_by": bubble | composer_cleared, "detail"}.
    Raises KakaoError (nothing typed) when the composer holds a draft, when the newest
    outgoing bubble already shows this text and allow_repeat is false, or when the
    window has no composer.
    """
    win = chat.require_window()
    field = composer(win)
    table = find(win, lambda e: attr(e, "AXRole") == "AXTable")
    if field is None or table is None:
        raise KakaoError("composer or chat table not found in the chat window")
    draft = attr(field, "AXValue") or ""
    if draft.strip():
        raise KakaoError(f"the composer already holds a draft; not overwriting it ({len(draft)} chars)")
    sent_n = normalize(text)
    tail = chat.messages(win, last_rows=TAIL_ROWS)
    outs = [m for m in tail if m.dir == "out"]
    if not allow_repeat and outs and same_text(outs[-1].text, sent_n):
        raise KakaoError("the newest outgoing bubble in this room already shows this text, so it was "
                         "probably sent already; pass allow_repeat=true to send it again")
    before = sum(1 for m in outs if same_text(m.text, sent_n))
    on_screen = any(w["title"] == chat.title and w["onscreen"] for w in list_windows(chat.pid))
    timeout = VERIFY_ON_SCREEN if on_screen else VERIFY_OFF_SCREEN
    rows0 = len(attr(table, "AXRows") or [])

    if AS.AXUIElementSetAttributeValue(field, "AXValue", text) != 0:
        raise KakaoError("could not set the composer text")
    button = None
    for _ in range(15):
        time.sleep(0.2)
        b = send_button(win)
        if b is not None and attr(b, "AXEnabled"):
            button = b
            break
    if button is None:
        AS.AXUIElementSetAttributeValue(field, "AXValue", "")
        return {"status": "not_sent", "detail": "전송 button never enabled; composer cleared"}
    press_err = AS.AXUIElementPerformAction(button, "AXPress")

    t0, cleared_at = time.time(), None
    while time.time() - t0 < timeout:
        time.sleep(0.6)
        grown = max(0, len(attr(table, "AXRows") or []) - rows0)
        msgs = chat.messages(win, last_rows=TAIL_ROWS + grown)
        if sum(1 for m in msgs if m.dir == "out" and same_text(m.text, sent_n)) > before:
            return {"status": "sent", "verified_by": "bubble",
                    "detail": f"bubble seen after {time.time() - t0:.1f}s (AXPress err={press_err})"}
        if cleared_at is None and not (attr(field, "AXValue") or "").strip():
            cleared_at = time.time()
        if cleared_at is not None and time.time() - cleared_at > AFTER_CLEARED:
            break
    left = (attr(field, "AXValue") or "").strip()
    if not left:
        return {"status": "sent", "verified_by": "composer_cleared",
                "detail": f"KakaoTalk cleared the composer; no matching bubble within {time.time() - t0:.1f}s "
                          f"(AXPress err={press_err}). Check the capture; do not resend."}
    if normalize(left) == sent_n:
        AS.AXUIElementSetAttributeValue(field, "AXValue", "")
        return {"status": "not_sent", "detail": f"the text stayed in the composer (AXPress err={press_err}); cleared"}
    return {"status": "uncertain", "detail": f"the composer now holds other text ({len(left)} chars); not touched. "
                                             "Check the capture; do not resend."}
