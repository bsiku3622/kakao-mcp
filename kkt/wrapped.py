"""'우정 Wrapped' - a one-page HTML report from a chat archive (JSONL from Watcher)."""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import datetime

MATH_WORDS = ("수학", "미적", "적분", "미분", "함수", "증명", "공식", "방정식", "행렬", "확률",
              "수능", "시험", "성적", "문제", "풀이", "계산", "수열", "극한", "벡터")
_LAUGH = re.compile(r"^[ㅋㅎ]+$")


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def words(text: str) -> list[str]:
    toks = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    return [t for t in toks if not _LAUGH.match(t)]


def side_stats(msgs: list[dict]) -> dict:
    texts = [m["text"] for m in msgs if m["kind"] == "text"]
    joined = " ".join(texts)
    return {
        "count": len(msgs),
        "media": sum(m["kind"] == "media" for m in msgs),
        "kk": joined.count("ㅋ"),
        "questions": joined.count("?"),
        "avg_len": round(sum(map(len, texts)) / len(texts), 1) if texts else 0,
        "longest": max(texts, key=len) if texts else "",
        "top": Counter(w for t in texts for w in words(t)).most_common(8),
        "math": sum(joined.count(w) for w in MATH_WORDS),
    }


def hours(msgs: list[dict]) -> list[int]:
    h = [0] * 24
    for m in msgs:
        if m.get("ts"):
            h[datetime.fromtimestamp(m["ts"]).hour] += 1
    return h


def render(room: str, rows: list[dict]) -> str:
    me = side_stats([r for r in rows if r["dir"] == "out"])
    you = side_stats([r for r in rows if r["dir"] == "in"])
    hist = hours(rows)
    e = html.escape
    total = me["count"] + you["count"] or 1

    def card(title, mine, theirs, note=""):
        return (f'<div class="card"><h3>{e(title)}</h3><div class="vs"><div><b>{e(str(mine))}</b>'
                f'<span>나</span></div><div><b>{e(str(theirs))}</b><span>{e(room)}</span></div></div>'
                f'{f"<p>{e(note)}</p>" if note else ""}</div>')

    def top(stats):
        return "".join(f"<li>{e(w)} <em>{n}</em></li>" for w, n in stats["top"]) or "<li>데이터 부족</li>"

    peak = max(hist)
    bars = "".join(f'<div class="bar" title="{h}시 {n}개" style="--v:{(n / peak) if peak else 0:.3f}"></div>'
                   for h, n in enumerate(hist))
    verdict = (f"{room} 쪽이 수학 얘기를 더 함" if you["math"] > me["math"] else
               "내가 수학 얘기를 더 함" if me["math"] > you["math"] else "수학 얘기 0 — 평화")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>우정 Wrapped</title><style>
:root{{--bg:#faf7f2;--fg:#1d1b18;--muted:#6f6a62;--card:#fff;--line:#e7e1d8;--accent:#f5c400}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#151412;--fg:#f1ede6;--muted:#a19a8f;--card:#201e1b;--line:#34312c;--accent:#f5c400}}}}
:root[data-theme=dark]{{--bg:#151412;--fg:#f1ede6;--muted:#a19a8f;--card:#201e1b;--line:#34312c;--accent:#f5c400}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,"Apple SD Gothic Neo",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:32px 16px 64px}} h1{{font-size:2rem;margin:0}} .sub{{color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}}
.card h3{{margin:0 0 8px;font-size:.95rem;color:var(--muted)}} .vs{{display:flex;gap:16px}}
.vs div{{flex:1}} .vs b{{display:block;font-size:1.8rem}} .vs span{{color:var(--muted);font-size:.85rem}}
.card p{{margin:8px 0 0;font-size:.9rem;overflow-wrap:anywhere}} ul{{margin:0;padding-left:18px}} em{{color:var(--muted);font-style:normal}}
.bars{{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;align-items:end;height:90px}}
.bar{{background:var(--accent);border-radius:3px 3px 0 0;height:calc(var(--v)*100% + 2px)}}
.verdict{{font-size:1.4rem;font-weight:700;margin-top:4px}}
</style></head><body><main>
<h1>우정 Wrapped</h1><div class="sub">{e(room)} · 메시지 {total}개 · kkt 아카이브 기준</div>
<div class="grid">
{card("보낸 메시지", me["count"], you["count"], f"점유율 나 {me['count'] * 100 // total}%")}
{card("ㅋ 개수", me["kk"], you["kk"])}
{card("평균 글자 수", me["avg_len"], you["avg_len"])}
{card("이모티콘·사진", me["media"], you["media"])}
{card("물음표", me["questions"], you["questions"])}
<div class="card"><h3>수학 단어 언급</h3><div class="vs"><div><b>{me["math"]}</b><span>나</span></div><div><b>{you["math"]}</b><span>{e(room)}</span></div></div><div class="verdict">{e(verdict)}</div></div>
<div class="card"><h3>내 단골 단어</h3><ul>{top(me)}</ul></div>
<div class="card"><h3>{e(room)} 단골 단어</h3><ul>{top(you)}</ul></div>
<div class="card"><h3>제일 긴 한 마디 — {e(room)}</h3><p>{e(you["longest"][:200])}</p></div>
<div class="card" style="grid-column:1/-1"><h3>시간대 (0–23시, 실시간 수집분만)</h3><div class="bars">{bars}</div></div>
</div></main></body></html>"""
