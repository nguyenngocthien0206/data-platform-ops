"""Sending pages and updates.

The toolkit's default is local: every notification is a row in
``ops.notifications`` and a line in the log, so a run is fully inspectable with
no account anywhere. Slack is optional. It is switched on only by setting
``SLACK_WEBHOOK_URL`` in the environment or in ``.env``, and it uses nothing but
the standard library. Tests never set it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import duckdb

from platform_ops.common.clock import SimulatedClock
from platform_ops.common.db import OPS_SCHEMA, insert_rows
from platform_ops.common.logging import get_logger, log_event

NotificationKind = Literal["page", "update", "resolved"]
SLACK_ENV_VAR = "SLACK_WEBHOOK_URL"
SLACK_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Notification:
    incident_id: str
    kind: NotificationKind
    recipient: str
    team: str
    severity: str
    text: str
    sent_at: datetime


class Notifier(Protocol):
    def send(self, notification: Notification) -> None: ...


NOTIFICATIONS_DDL = """
    incident_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    recipient VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    sent_at TIMESTAMP NOT NULL"""

NOTIFICATION_COLUMNS = ("incident_id", "kind", "recipient", "team", "severity", "text", "sent_at")


def reset_notifications(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.notifications ({NOTIFICATIONS_DDL})")


class LocalNotifier:
    """Logs each notification and buffers it for ``ops.notifications``.

    Rows are written by :meth:`flush`, once per run, so a run's notifications
    share the transaction the rest of the run's writes use.
    """

    def __init__(self) -> None:
        self.logger = get_logger("incidents")
        self.pending: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.pending.append(notification)
        log_event(
            self.logger,
            f"{notification.kind} {notification.incident_id} to {notification.recipient}: "
            f"{notification.text}",
            clock=SimulatedClock(notification.sent_at),
        )

    def flush(self, connection: duckdb.DuckDBPyConnection) -> int:
        rows = [
            (n.incident_id, n.kind, n.recipient, n.team, n.severity, n.text, n.sent_at)
            for n in self.pending
        ]
        self.pending.clear()
        return insert_rows(connection, f"{OPS_SCHEMA}.notifications", NOTIFICATION_COLUMNS, rows)


class SlackNotifier:
    """Posts to a Slack incoming webhook. A failed post is logged, never raised:
    a chat outage must not stop incident tracking."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self.logger = get_logger("incidents.slack")

    def send(self, notification: Notification) -> None:
        payload = {
            "text": f"[{notification.severity}] {notification.incident_id} "
            f"({notification.kind}) @{notification.recipient}: {notification.text}"
        }
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS):  # noqa: S310
                pass
        except (urllib.error.URLError, TimeoutError) as error:
            self.logger.warning("Slack post for %s failed: %s", notification.incident_id, error)


class CompositeNotifier:
    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self.notifiers = tuple(notifiers)

    def send(self, notification: Notification) -> None:
        for notifier in self.notifiers:
            notifier.send(notification)


def slack_webhook_url(env_file: Path) -> str | None:
    """The webhook from the environment, else from ``.env``, else ``None``."""
    from_env = os.environ.get(SLACK_ENV_VAR, "").strip()
    if from_env:
        return from_env
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == SLACK_ENV_VAR and not name.startswith("#"):
            return value.strip().strip("'\"") or None
    return None


def build_notifier(local: LocalNotifier, env_file: Path) -> Notifier:
    url = slack_webhook_url(env_file)
    return local if url is None else CompositeNotifier([local, SlackNotifier(url)])
