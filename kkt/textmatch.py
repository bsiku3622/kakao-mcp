"""Compare a sent text with what a chat shows - shared by the macOS and Windows backends.

KakaoTalk folds long messages (about 500+ characters, ending in "…" or "..."), and
line breaks come back as other separators, so texts are compared after
normalising whitespace, by their opening characters.
"""
from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Text as it renders: NFC, any line break or run of spaces as one space."""
    s = unicodedata.normalize("NFC", s or "").replace(" ", "\n").replace(" ", "\n")
    return _WS.sub(" ", s).strip()


def same_text(bubble: str, sent_n: str) -> bool:
    """A bubble shows the sent text: equal after normalising, the same opening 40
    characters (folded long bubbles), or a long-enough leading part of it."""
    b = normalize(bubble)
    for mark in ("…", "..."):
        if b.endswith(mark):
            b = b[: -len(mark)].rstrip()
    if not b:
        return False
    if b == sent_n:
        return True
    head = min(40, len(sent_n))
    if len(b) >= head and b[:head] == sent_n[:head]:
        return True
    return len(b) >= 20 and sent_n.startswith(b)
