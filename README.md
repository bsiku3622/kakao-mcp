# kakao-mcp

An MCP server and CLI (`kkt`) that read and send KakaoTalk for Mac messages through
the Accessibility API, so Claude, Codex or any MCP client can message deterministically.
No keystrokes, no mouse events, no focus stealing, no network traffic of its own:
everything is AX attribute reads, `AXValue` writes and `AXPress` on the official app,
plus background window captures.

Inspired by [openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli),
[kakaocli](https://github.com/silver-flight-group/kakaocli) and
[KiwiTalk](https://github.com/KiwiTalk/KiwiTalk). Unlike KiwiTalk it never logs in
as a separate client, and unlike kakaocli it does not touch the local database.

## Setup

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The process that runs `bin/kkt` needs **Accessibility** and **Screen Recording**
permission (a terminal that already has them is enough).

## Commands

| Command | What it does |
| --- | --- |
| `bin/kkt rooms` | List open KakaoTalk windows |
| `bin/kkt read ROOM [-n 20]` | Print recent messages (`<` incoming, `>` outgoing) |
| `bin/kkt send ROOM TEXT` | Send a message and verify it appears |
| `bin/kkt snap ROOM [--full]` | Capture the chat window in the background (reactions and stickers live only here) |
| `bin/kkt watch ROOM` | One tab-separated line per burst of new activity, with a capture path |
| `bin/kkt bot ROOM [--persona FILE] [--dry-run]` | Persona bot that answers each burst through a tool-less `claude -p` (`personas/example.md` by default; other files in `personas/` stay out of git) |
| `bin/kkt wrapped ROOM` | Render a one-page "우정 Wrapped" HTML from the archive |

`ROOM` is the chat window title. The chat must be open in its own window.

## MCP server

`bin/kkt-mcp` exposes the same AX paths as MCP tools. The model never drives the
UI; it calls a tool, gets a verdict computed from the app, and gets a window
capture for a visual second look.

### Recommended: one daemon, every client through a bridge

macOS gives Accessibility and Screen Recording to the app responsible for a
process. The daemon therefore runs as a child of a tiny launcher app,
**Kakao MCP**, which is the only thing you allow. It listens on `127.0.0.1:8799`
behind a bearer token kept in `data/http-token` (mode 600), so nothing is exposed
on the network, and one process serializes every KakaoTalk operation.

```sh
scripts/install-daemon.sh
```

This builds `build/Kakao MCP.app`, links `~/.local/bin/kakao-mcp`, and starts the
daemon at login through launchd (`com.bsiku3622.kakao-mcp`). Then allow
**Kakao MCP** in System Settings → Privacy & Security → **Accessibility** and
**Screen & System Audio Recording**. Rebuilding the launcher changes its ad-hoc
signature, so macOS may ask again.

Clients start `kakao-mcp --bridge`, a stdio relay to the daemon that reads the
token itself and needs no permissions:

```sh
# Claude Code on the Mac
claude mcp add kakao -s user -- ~/.local/bin/kakao-mcp --bridge
```

```toml
# Codex (~/.codex/config.toml)
[mcp_servers.kakao]
command = "/Users/you/.local/bin/kakao-mcp"
args = ["--bridge"]
```

Another machine reaches the same daemon over SSH, with no port to open:

```sh
# Claude Code on another computer that can `ssh mac`
claude mcp add kakao -s user -- ssh -T -o BatchMode=yes mac /Users/you/.local/bin/kakao-mcp --bridge
```

### Without the daemon

`bin/kkt-mcp` with no flag is a plain stdio server that drives KakaoTalk itself.
It then needs the caller's permissions (a terminal that already has them is
enough). A cross-process lock keeps it and the daemon from typing at once.

| Tool | What it does |
| --- | --- |
| `kakao_status` | Running, permissions, screen lock, open rooms, guardrail settings |
| `list_rooms` | Chat rooms open in their own window (any desktop) |
| `read_messages(room, limit)` | Recent messages, `in` / `out`, `text` / `media` |
| `capture_room(room, full)` | Background capture of the chat window |
| `open_room(room)` | Opens a chat from the chat list: search via `AXSelectedText`, then the row's context menu item `채팅방 열기` |
| `send_message(room, text, …)` | Sends, verifies, returns `sent` / `not_sent` / `uncertain` / `refused` plus a capture |

A send counts as `sent` only when an outgoing bubble with the exact text shows up
among rows that did not exist before `AXPress`, so an identical earlier message
cannot pass. `not_sent` means the text was still in the composer (it is cleared).
`uncertain` means neither; the server tells the model not to resend.

Guardrails: `KKT_ALLOWED_ROOMS` (comma-separated allowlist), `KKT_MAX_PER_HOUR`
(default 30), `KKT_MIN_INTERVAL` (default 2 s), `KKT_REPEAT_WINDOW` (same text to
the same room, default 120 s), a 1000-character limit, `data/STOP`, and drafts are
never overwritten. Every send attempt is logged to `data/mcp-log.jsonl`.

`open_room` follows openkakao-cli's context-menu route
([JungHoonGhae/openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli), MIT).
The menu only appears while KakaoTalk's main window is on a desktop being shown
and the keyboard and mouse have been idle for 2 s, so otherwise it refuses.
Sending to a room that is already open works from any desktop.

## How it works

- **Finding the window.** KakaoTalk leaves `AXWindows` empty, so the chat window is
  found via `AXFocusedWindow`/`AXMainWindow` or, failing that, the private
  `_AXUIElementCreateWithRemoteToken` scan used by alt-tab. The element id is cached
  in `data/winid_cache.json`.
- **Direction.** Incoming bubbles hug the left edge, outgoing ones the right.
- **Reactions and stickers.** Reaction emoji are not exposed through AX; a reaction
  shows up as a change in the row's height and buttons, and `snap` shows which one.
- **Bursts.** New incoming messages are batched until the chat has been quiet for a
  few seconds, so ten one-word lines get one reply. Messages that arrived while
  nobody was watching (for example while the screen was locked) form the first burst.
- **Cheap polling.** KakaoTalk answers AX on its main thread and walking rows is
  expensive, so the watcher only reads the row count every 2 s and walks the last
  25 rows when it changes (and every 30 s for reactions).
- **The bot can see.** When a burst has a sticker/photo or a reaction, the bot
  captures the window and sends the image inline (base64 over `stream-json`), so
  the model still needs no file-reading tool.

## Bot guardrails

- Only the room it was started for.
- `claude -p` runs with `--tools ""`, no MCP servers and no session persistence, in
  an empty directory. Chat messages can attempt prompt injection; the only output
  is reply text.
- One reply per burst, `--min-interval` seconds apart, `--max-per-hour` cap.
- `touch data/STOP` stops `watch` and `bot`. A draft already in the input field is
  never overwritten.

## Limits

- **Screen lock.** While the screen is locked, macOS returns nothing for other apps'
  AX attributes, so nothing works until it is unlocked (`watch`/`bot` wait and resume,
  `send_message` refuses). Keeping the display awake (for example `caffeinate -d`)
  avoids the display-off lock; closing the lid or locking by hand still locks.
- **One PC at a time.** KakaoTalk keeps one PC session (Windows or Mac) signed in, so
  a second desktop client would sign this Mac out. Other machines go through the Mac.
- Only messages loaded in the window are visible; the archive grows from the moment
  `watch` or `bot` starts.
- Automating KakaoTalk may conflict with Kakao's policies. Keep the volume low.

## License

MIT © Jaewon Baek
