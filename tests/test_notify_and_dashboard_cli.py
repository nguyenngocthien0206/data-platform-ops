"""The outward-facing edges: posting to Slack and Telegram, and launching Streamlit.

None touches the outside world here. Posts go to a throwaway HTTP server on
127.0.0.1 standing in for both APIs, and the Streamlit process is replaced by a
stub, so the tests pin what is sent without sending it anywhere.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from platform_ops import cli
from platform_ops.common.config import CONFIG_PATH_ENV_VAR
from platform_ops.incidents.notify import Notification, SlackChannel, TelegramChannel, deliver
from platform_ops.metadata.registry import Team

NOTE = Notification(
    "INC-001",
    "page",
    "night-2026-04-07",
    "priya",
    "platform",
    "SEV1",
    "2 checks failing",
    datetime(2026, 4, 7, 2),
)
TEAM = Team(
    id="platform", name="P", channel="#data-platform", members=["priya"], telegram_chat="-100"
)


@pytest.fixture
def api() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """One local server answering like both chat APIs, recording every post."""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            reply = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()


def test_slack_posts_to_the_teams_channel_with_the_bot_token(
    api: tuple[str, list[dict[str, Any]]],
) -> None:
    url, received = api
    assert deliver(NOTE, TEAM, [SlackChannel("xoxb-1", url)]) == "slack"
    assert received == [
        {
            "path": "/chat.postMessage",
            "auth": "Bearer xoxb-1",
            "body": {
                "channel": "#data-platform",
                "text": "[SEV1] INC-001 (page) for priya: 2 checks failing",
            },
        }
    ]


def test_telegram_posts_to_the_teams_chat(api: tuple[str, list[dict[str, Any]]]) -> None:
    url, received = api
    channels = [SlackChannel("xoxb-1", url), TelegramChannel("123:abc", url)]
    assert deliver(NOTE, TEAM, channels) == "slack, telegram"
    telegram = received[1]
    assert telegram["path"] == "/bot123:abc/sendMessage" and telegram["auth"] is None
    assert telegram["body"] == {
        "chat_id": "-100",
        "text": "[SEV1] INC-001 (page) for priya: 2 checks failing",
    }


def test_a_failed_post_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    # Nothing listens on port 9 (discard) of 127.0.0.1 here.
    assert deliver(NOTE, TEAM, [SlackChannel("xoxb-1", "http://127.0.0.1:9")]) == "slack failed"
    assert "slack for INC-001 failed" in caplog.text


def test_dashboard_runs_streamlit_headless_without_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD_DASHBOARD", "a-password")
    launched: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command: list[str], **_: object) -> None:
            launched.append(command)

        def poll(self) -> int | None:
            return 0

        def wait(self) -> int:
            return 0

    monkeypatch.setattr("subprocess.Popen", FakeProcess)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = CliRunner().invoke(cli.app, ["dashboard", "--headless", "--port", "8765"])
    assert result.exit_code == 0, result.output
    (command,) = launched
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert command[4].endswith("app.py")
    options = dict(zip(command[5::2], command[6::2], strict=True))
    assert options == {
        "--server.port": "8765",
        "--server.headless": "true",
        "--browser.gatherUsageStats": "false",
    }
    assert opened == [], "--headless never opens a browser"


def test_dashboard_without_its_password_exits_2_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, settings_file: Path
) -> None:
    """The pages read as their own role: no password, no Streamlit."""
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings_file))
    monkeypatch.delenv("POSTGRES_PASSWORD_DASHBOARD", raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda *_, **__: pytest.fail("Streamlit started"))
    result = CliRunner().invoke(cli.app, ["dashboard", "--headless"])
    assert result.exit_code == 2
    assert "POSTGRES_PASSWORD_DASHBOARD" in result.output
