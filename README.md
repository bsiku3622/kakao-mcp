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
| `bin/kkt send ROOM TEXT [--allow-repeat]` | Send a message and print the verdict (`sent bubble …`) |
| `bin/kkt snap ROOM [--full]` | Capture the chat window in the background (reactions and stickers live only here) |
| `bin/kkt watch ROOM [--skip-backlog]` | One tab-separated line per burst of new activity, with a capture path; `--skip-backlog` starts from what is on screen instead of releasing unanswered messages first |
| `bin/kkt bot ROOM [--persona FILE] [--dry-run]` | Persona bot that answers each burst through a tool-less `claude -p` (`personas/example.md` by default; other files in `personas/` stay out of git) |
| `bin/kkt wrapped ROOM` | Render a one-page "우정 Wrapped" HTML from the archive |

`ROOM` is the chat window title. The CLI needs the chat open in its own window; the MCP server opens it.

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

The daemon serves stateless HTTP, so restarting it (`launchctl kickstart -k
gui/$UID/com.bsiku3622.kakao-mcp`) does not break bridges that are connected.

Clients start `kakao-mcp --bridge`, a stdio relay to the daemon that reads the
token itself and needs no permissions. It exits when the client that started it
goes away:

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

On Windows, use Git for Windows' `C:\Program Files\Git\usr\bin\ssh.exe`, not
`C:\Windows\System32\OpenSSH\ssh.exe`: the built-in client does not forward
piped stdin while it stays open, so an MCP client gets no reply and times out.
The same command works in Claude Desktop's `claude_desktop_config.json` and in
Codex's `config.toml` (`command = 'C:\Program Files\Git\usr\bin\ssh.exe'`).

### Without the daemon

`bin/kkt-mcp` with no flag is a plain stdio server that drives KakaoTalk itself.
It then needs the caller's permissions (a terminal that already has them is
enough). A cross-process lock keeps it and the daemon from typing at once.

| Tool | What it does |
| --- | --- |
| `kakao_status` | Running, permissions, screen lock, open rooms |
| `list_rooms` | Chat rooms open in their own window (any desktop; windows without a composer are left out) |
| `read_messages(room, limit)` | Recent messages, `in` / `out`, `text` / `media` |
| `capture_room(room, full)` | Background capture of the chat window |
| `open_room(room)` | Opens a chat ahead of time (the other tools open rooms by themselves) |
| `send_message(room, text, …)` | Opens the room if needed, sends, verifies, returns `sent` / `not_sent` / `uncertain` / `refused` plus a capture |

A room is named as the chat list shows it, without a `팀채팅, ` / `오픈채팅, ` /
`나와의 채팅, ` prefix. When a name matches no single row, the error lists the rooms
KakaoTalk's search found.

### Verdicts

`sent` comes in two kinds. `verified_by: bubble` means more outgoing bubbles show
the text in the newest rows than before the press. Text is compared after
normalising whitespace and line breaks, by its opening 40 characters, so bubbles
folded behind 전체보기 (about 500 characters) and messages followed by a link card
still verify. `verified_by: composer_cleared` means no bubble matched in time, but
KakaoTalk emptied the composer, which it does only when it sends. `not_sent` means
the text stayed in the composer (it is cleared). `uncertain` means the composer now
holds other text. The server tells the model never to resend after `sent` or
`uncertain`.

A send is refused when the room's newest outgoing bubble already shows the same
text, which catches a repeat made through another tool after an interrupted call;
`allow_repeat=true` sends it anyway. Drafts are never overwritten. Optional
switches: `KKT_ALLOWED_ROOMS` (comma-separated allowlist) and `data/STOP`. Every
send attempt is logged to `data/mcp-log.jsonl`. The CLI (`kkt send`) and the bot go
through the same code (`kkt/deliver.py`).

### Opening a room that has no window

KakaoTalk 26.10.0 exposes one element-scoped way to open a chat: the chat row's
context menu item `채팅방 열기` (rows, cells and their buttons reject `AXPress`,
`AXOpen`, `AXConfirm` and `AXShowDefaultUI`, and the search field's `AXConfirm` opens
nothing). The route follows openkakao-cli
([JungHoonGhae/openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli), MIT):
filter the list with `AXSelectedText` (setting `AXValue` does not filter), take the
one row named exactly so, `AXShowMenu` on its cell (the menu shows up as a child of
the cell), and `AXPress` the item.

The menu is only drawn while the main window is on a desktop being shown. When it
is not, the app is brought forward with `AXFrontmost` (macOS then shows its
desktop), the room is opened, and the app that was in front is handed back the
same way, checked until it sticks - about a second, and only after the keyboard and
mouse have rested for 1.5 s. A room stays open afterwards, so later sends cause no
visible change.

## How it works

- **Finding the window.** KakaoTalk leaves `AXWindows` empty, so the chat window is
  found via `AXFocusedWindow`/`AXMainWindow` or, failing that, the private
  `_AXUIElementCreateWithRemoteToken` scan used by alt-tab. The element id is cached
  in `data/winid_cache.json`.
- **Direction.** A bubble is outgoing when it sits closer to the right edge than the
  left, incoming otherwise; rows with nearly equal margins are separators. Fixed
  margins broke once a scroll bar widened the right margin.
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
