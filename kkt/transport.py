"""How a kakao-mcp server is reached - shared by the macOS and Windows servers.

  (no flag)   stdio: the process that runs the tools talks to one client.
  --http      daemon on 127.0.0.1:<port>, stateless HTTP with JSON replies, behind a
              bearer token kept in <data>/http-token. Stateless, so a restarted daemon
              keeps serving bridges that were already connected.
  --bridge    stdio relay to that daemon for clients that only speak stdio. It never
              touches KakaoTalk itself.
"""
from __future__ import annotations

import argparse
import hmac
import os
import secrets
import sys


def token(data_dir: str, create: bool) -> str:
    path = os.path.join(data_dir, "http-token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    if not create:
        sys.exit(f"kakao-mcp: no daemon token at {path}; start the daemon first")
    os.makedirs(data_dir, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


class BearerGate:
    """ASGI guard: every HTTP request must carry the daemon's bearer token."""

    def __init__(self, app, tok: str):
        self.app, self.expected = app, f"Bearer {tok}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def run_http(mcp, data_dir: str, port: int) -> None:
    import uvicorn
    app = BearerGate(mcp.streamable_http_app(host="127.0.0.1", stateless_http=True, json_response=True),
                     token(data_dir, create=True))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run_bridge(data_dir: str, port: int) -> None:
    """Relay stdio <-> the local daemon, message for message."""
    import logging

    import anyio
    for name in ("httpx", "httpx2", "httpcore", "mcp.client"):   # INFO lines per request otherwise
        logging.getLogger(name).setLevel(logging.WARNING)
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    from mcp.server.stdio import stdio_server

    async def relay():
        import httpx2
        headers = {"Authorization": f"Bearer {token(data_dir, create=False)}"}
        # Opening a room can wait for the user to rest, then send and verify.
        timeout = httpx2.Timeout(300.0, connect=10.0)
        async with create_mcp_http_client(headers=headers, timeout=timeout) as http, \
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r_up, w_up), \
                stdio_server() as (r_in, w_in):
            async with anyio.create_task_group() as tg:
                async def pump(src, dst, label):
                    async for msg in src:
                        if isinstance(msg, Exception):
                            print(f"kakao-mcp bridge ({label}): {msg}", file=sys.stderr)
                            continue
                        await dst.send(msg)
                    tg.cancel_scope.cancel()

                async def orphan_watch(parent=os.getppid()):
                    if os.name != "posix":        # Windows does not reparent; stdin EOF ends us there
                        return
                    while os.getppid() == parent:     # reparented to launchd/init = client died
                        await anyio.sleep(2)
                    tg.cancel_scope.cancel()
                tg.start_soon(pump, r_in, w_up, "client->daemon")
                tg.start_soon(pump, r_up, w_in, "daemon->client")
                tg.start_soon(orphan_watch)

    anyio.run(relay)


def main(mcp, data_dir: str, port: int, prepare_daemon=None) -> None:
    if sys.stdout is None or sys.stderr is None:
        # pythonw (the Windows logon task) has no console; uvicorn's first log line
        # would crash the daemon, so both streams go to data/daemon.log.
        os.makedirs(data_dir, exist_ok=True)
        log = open(os.path.join(data_dir, "daemon.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    p = argparse.ArgumentParser(prog="kakao-mcp")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--http", action="store_true", help="run the local daemon on 127.0.0.1")
    g.add_argument("--bridge", action="store_true", help="relay stdio to the local daemon")
    a = p.parse_args()
    if a.http:
        if prepare_daemon:
            prepare_daemon()
        run_http(mcp, data_dir, port)
    elif a.bridge:
        run_bridge(data_dir, port)
    else:
        mcp.run("stdio")
