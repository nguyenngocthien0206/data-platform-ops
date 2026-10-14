"""The two outward-facing edges: posting to Slack, and launching Streamlit.

Neither touches the outside world here. Slack posts go to a throwaway HTTP
server on 127.0.0.1, and the Streamlit process is replaced by a stub, so the
tests pin what is sent without sending it anywhere.
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
from platform_ops.incidents.notify import CompositeNotifier, Notification, SlackNotifier

NOTE = Notification(
    "INC-001", "page", "priya", "platform", "SEV1", "2 checks failing", datetime(2026, 1, 8, 2)
)


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", received
    finally:
        server.shutdown()


def test_slack_posts_the_page_as_json(webhook: tuple[str, list[dict[str, Any]]]) -> None:
    url, received = webhook
    SlackNotifier(url).send(NOTE)
    assert received == [{"text": "[SEV1] INC-001 (page) @priya: 2 checks failing"}]


def test_a_failed_post_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    # Nothing listens on port 9 (discard) of 127.0.0.1 here.
    SlackNotifier("http://127.0.0.1:9/hook").send(NOTE)
    assert "Slack post for INC-001 failed" in caplog.text


def test_composite_sends_to_every_notifier() -> None:
    seen: list[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def send(self, notification: Notification) -> None:
            seen.append(f"{self.name}:{notification.incident_id}")

    CompositeNotifier([Recorder("a"), Recorder("b")]).send(NOTE)
    assert seen == ["a:INC-001", "b:INC-001"]


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
