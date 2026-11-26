"""Persona bot: watch one chat and banter back through a tool-less `claude -p`.

Guardrails
- Works on the single room it was started for.
- The model runs with every built-in tool disabled, no MCP servers, no session
  persistence, inside an empty working directory. Messages may try prompt
  injection, but the only thing that can come out is reply text.
- One reply per burst, a minimum interval between replies, an hourly cap.
- `touch <data>/STOP` stops it; a draft already in the input field is never
  overwritten.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time

from .ax import Chat, KakaoError, Message
from .capture import snap
from .watch import Event, Watcher

MAX_REPLY_CHARS = 300
MAX_MSG_CHARS = 300   # long spam ("read this and burn your tokens") is cut before the model sees it


def _clip(text: str) -> str:
    return text if len(text) <= MAX_MSG_CHARS else text[:MAX_MSG_CHARS] + f"…(+{len(text) - MAX_MSG_CHARS}자 생략)"


def collapse(msgs: list[Message]) -> list[tuple[Message, int]]:
    """Fold runs of identical consecutive messages (spam) into (message, count)."""
    out: list[tuple[Message, int]] = []
    for m in msgs:
        if out and out[-1][0].key == m.key:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((m, 1))
    return out


def _line(m: Message, n: int) -> str:
    return _clip(m.text) + (f" (같은 메시지 ×{n} 도배)" if n > 1 else "")


def render(history: list[Message], n: int = 20) -> str:
    who = {"in": "상대", "out": "나"}
    return "\n".join(f"[{who[m.dir]}] {_line(m, k)}" for m, k in collapse(history)[-n:])


def build_prompt(history: list[Message], ev: Event, with_image: bool = False) -> str:
    parts = ["## 최근 대화 (위가 오래된 것)", render(history)]
    if ev.reactions:
        parts += ["## 상대가 이모지 공감을 단 내 메시지", "\n".join(f"- {_clip(m.text)}" for m in ev.reactions)]
    if ev.incoming:
        got = collapse(ev.incoming)
        skipped = f"\n- …(앞의 {len(got) - 10}묶음 생략)" if len(got) > 10 else ""
        parts += ["## 방금 상대가 보낸 것", skipped.strip() + "\n".join(f"- {_line(m, k)}" for m, k in got[-10:])]
    if with_image:
        parts.append("첨부 이미지는 지금 대화창 하단 캡처다. [이모티콘/사진]이 실제로 무슨 그림인지, "
                     "내 노란 말풍선 아래 붙은 공감 이모지(이모지+숫자)가 뭔지 보고 답장에 활용해.")
    parts.append("위 상황에 대한 내 답장 하나만 출력해.")
    return "\n\n".join(parts)


def clean(text: str) -> str:
    text = text.strip().strip('"“”').strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_REPLY_CHARS]


def generate(persona: str, prompt: str, model: str, sandbox: str, image: str | None = None,
             timeout: int = 120) -> str:
    """One tool-less claude -p turn. The image (if any) is inlined as a base64 content
    block via stream-json, so the model never needs a file-reading tool."""
    os.makedirs(sandbox, exist_ok=True)
    content = []
    if image:
        with open(image, "rb") as f:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(f.read()).decode()}})
    content.append({"type": "text", "text": prompt})
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--tools", "", "--strict-mcp-config", "--setting-sources", "local", "--no-session-persistence",
           "--model", model, "--system-prompt", persona]
    r = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                       timeout=timeout, cwd=sandbox)
    result = None
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "result":
            result = o
    if r.returncode != 0 or result is None or result.get("is_error"):
        raise RuntimeError(f"claude -p failed ({r.returncode}): {(r.stderr or str(result))[-300:]}")
    reply = clean(result.get("result") or "")
    if not reply:
        raise RuntimeError("empty reply")
    return reply


def run(room: str, persona_path: str, data_dir: str, model: str = "claude-haiku-5-5",
        min_interval: float = 15, max_per_hour: int = 30, quiet: float = 4.0, dry_run: bool = False):
    safe = re.sub(r"[^\w가-힣ㄱ-ㅎㅏ-ㅣ-]+", "_", room)
    chat = Chat(room, cache_path=os.path.join(data_dir, "winid_cache.json"))
    watcher = Watcher(chat, quiet=quiet, archive=os.path.join(data_dir, "archive", f"{safe}.jsonl"),
                      stop_file=os.path.join(data_dir, "STOP"))
    persona = open(persona_path, encoding="utf-8").read()
    log_path = os.path.join(data_dir, "bot-log.jsonl")
    sandbox = os.path.join(data_dir, "sandbox")
    sent: list[float] = []

    def log(**kw):
        kw["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
        line = json.dumps(kw, ensure_ascii=False)
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(event="start", room=room, model=model, dry_run=dry_run)
    for ev in watcher.events():
        if ev.type == "stop":
            log(event="stop")
            return
        if ev.type != "burst":
            log(event=ev.type, detail=ev.detail)
            continue
        now = time.time()
        sent[:] = [t for t in sent if now - t < 3600]
        if len(sent) >= max_per_hour:
            log(event="skip", reason="hourly cap", incoming=[m.text[:80] for m in ev.incoming][-10:])
            continue
        if sent and now - sent[-1] < min_interval:
            time.sleep(min_interval - (now - sent[-1]))
        image = None
        if any(m.kind == "media" for m in ev.incoming) or ev.reactions:
            wid = chat.cg_window_id()
            if wid is not None:
                try:
                    image = snap(wid, os.path.join(data_dir, "caps", f"bot-{time.strftime('%H%M%S')}.png"))
                except Exception as e:  # a reply without the picture is still fine
                    log(event="capture-failed", detail=str(e)[:200])
        try:
            reply = generate(persona, build_prompt(watcher.history(), ev, with_image=bool(image)),
                             model, sandbox, image=image)
            if not dry_run:
                chat.send(reply)
            sent.append(time.time())
            log(event="reply", incoming=[_line(m, k)[:80] for m, k in collapse(ev.incoming)][-10:],
                reactions=[m.text[:80] for m in ev.reactions], reply=reply, sent=not dry_run)
        except (KakaoError, RuntimeError, subprocess.TimeoutExpired) as e:
            log(event="error", detail=str(e)[:300])
