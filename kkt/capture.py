"""Background capture of one window (no activation) - the only way to see
reactions and stickers, which KakaoTalk does not expose through AX."""
from __future__ import annotations

import os
import subprocess
import tempfile

from PIL import Image


def snap(cg_window_id: int, out_path: str, crop: tuple[float, float] | None = (0.45, 0.86)) -> str:
    """Save a PNG of the window; crop keeps a vertical band (fractions of height)."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fd, raw = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", "-o", "-l", str(cg_window_id), raw],
                       check=True, timeout=10, capture_output=True)
        im = Image.open(raw)
        if crop:
            w, h = im.size
            im = im.crop((0, int(h * crop[0]), w, int(h * crop[1])))
        im.save(out_path)
    finally:
        os.unlink(raw)
    return out_path
