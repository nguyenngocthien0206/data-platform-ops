"""A stand-in for Slack and Telegram, so pages can be checked without sending them anywhere.

The orchestrator integration tests (Phase 14b) point the toolkit's Slack and
Telegram channels at it (``SLACK_API_URL``, ``TELEGRAM_API_URL``). It answers
the two calls the toolkit makes the way the real APIs do, and keeps every
message so a test can read them back:

- ``POST /chat.postMessage``: Slack, with a bot token in the ``Authorization``
  header and ``{"channel", "text"}``; answers ``{"ok": true}``;
- ``POST /bot<token>/sendMessage``: Telegram, with ``{"chat_id", "text"}``;
  answers ``{"ok": true, "result": {...}}``;
- ``GET /messages``: every message so far, oldest first; ``DELETE /messages``
  forgets them; ``GET /health`` answers ``ok``.

A request without a token is refused the way the real API refuses it, so a
missing token shows up in a test instead of passing silently. Standard library
only, so it runs in the toolkit's image or any Python::

    python orchestrators/chat_double.py --host 0.0.0.0 --port 8099
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TELEGRAM_PATH = re.compile(r"^/bot(?P<token>[^/]+)/sendMessage$")


class Messages:
    """Every message received, in order, shared by the server's threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def add(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(message)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class Handler(BaseHTTPRequestHandler):
    messages: Messages

    def _reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        found = json.loads(self.rfile.read(length) or b"{}")
        return found if isinstance(found, dict) else {}

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, "ok")
        elif self.path == "/messages":
            self._reply(200, self.messages.all())
        else:
            self._reply(404, {"ok": False, "error": "not_found"})

    def do_DELETE(self) -> None:
        if self.path == "/messages":
            self.messages.clear()
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/chat.postMessage":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not auth.removeprefix("Bearer ").strip():
                self._reply(200, {"ok": False, "error": "not_authed"})
                return
            self.messages.add(
                {"service": "slack", "to": body.get("channel"), "text": body.get("text")}
            )
            self._reply(200, {"ok": True})
            return
        telegram = TELEGRAM_PATH.match(self.path)
        if telegram:
            if not body.get("chat_id"):
                self._reply(400, {"ok": False, "description": "Bad Request: chat_id is empty"})
                return
            self.messages.add(
                {"service": "telegram", "to": str(body.get("chat_id")), "text": body.get("text")}
            )
            self._reply(200, {"ok": True, "result": {"message_id": len(self.messages.all())}})
            return
        self._reply(404, {"ok": False, "error": "unknown_method"})

    def log_message(self, *args: object) -> None:
        pass


def serve(host: str, port: int) -> tuple[ThreadingHTTPServer, Messages]:
    """A running double on ``host:port`` (port 0 picks a free one), and its messages."""
    messages = Messages()
    handler = type("BoundHandler", (Handler,), {"messages": messages})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, messages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    server, _ = serve(args.host, args.port)
    print(f"chat double on http://{args.host}:{server.server_port}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
