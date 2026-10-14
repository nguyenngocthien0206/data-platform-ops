"""Sending pages, updates and resolutions to each team, where it takes them (Phase 14).

Every notification is a row in ``incident_notifications`` first; the desk sends
it only after the incident is committed, then records how it went. With no
token configured that is ``local``: the row and a line in the log, which is
how the simulated company runs and how the tests run.

Two channels, each switched on by its bot token, each addressed per team from
``teams.yaml``:

- **Slack**: ``chat.postMessage`` to the team's ``channel``, with
  ``SLACK_BOT_TOKEN``;
- **Telegram**: ``sendMessage`` to the team's ``telegram_chat``, with
  ``TELEGRAM_BOT_TOKEN``.

Their API base URLs can be overridden (``SLACK_API_URL``, ``TELEGRAM_API_URL``),
which is how the tests point them at a local double. Nothing but the standard
library is used. A failed send is logged and recorded, never raised: a chat
outage must not stop incident tracking.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from platform_ops.common.env import env_value
from platform_ops.common.logging import get_logger
from platform_ops.metadata.registry import Team

NotificationKind = Literal["page", "update", "resolved"]
TIMEOUT_SECONDS = 5
SLACK_API_URL = "https://slack.com/api"
TELEGRAM_API_URL = "https://api.telegram.org"


@dataclass(frozen=True)
class Notification:
    incident_id: str
    kind: str
    run_id: str
    recipient: str
    team: str
    severity: str
    text: str
    sent_at: datetime

    def message(self) -> str:
        return (
            f"[{self.severity}] {self.incident_id} ({self.kind}) for {self.recipient}: {self.text}"
        )


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    def send(self, notification: Notification, team: Team) -> bool:
        """Send to the team's address here; ``False`` when the team has none."""
        ...


def _post(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body: dict[str, object] = json.loads(response.read().decode("utf-8") or "{}")
        return body


@dataclass(frozen=True)
class SlackChannel:
    token: str
    api_url: str = SLACK_API_URL
    name: str = "slack"

    def send(self, notification: Notification, team: Team) -> bool:
        body = _post(
            f"{self.api_url}/chat.postMessage",
            {"channel": team.channel, "text": notification.message()},
            {"Authorization": f"Bearer {self.token}"},
        )
        if not body.get("ok"):
            raise RuntimeError(f"Slack said {body.get('error', 'not ok')}")
        return True


@dataclass(frozen=True)
class TelegramChannel:
    token: str
    api_url: str = TELEGRAM_API_URL
    name: str = "telegram"

    def send(self, notification: Notification, team: Team) -> bool:
        if not team.telegram_chat:
            return False
        body = _post(
            f"{self.api_url}/bot{self.token}/sendMessage",
            {"chat_id": team.telegram_chat, "text": notification.message()},
            {},
        )
        if not body.get("ok"):
            raise RuntimeError(f"Telegram said {body.get('description', 'not ok')}")
        return True


def channels_from_env(env_file: Path) -> list[Channel]:
    """The channels whose tokens are set, in the environment or ``.env``."""
    found: list[Channel] = []
    slack = env_value("SLACK_BOT_TOKEN", env_file)
    if slack:
        found.append(SlackChannel(slack, env_value("SLACK_API_URL", env_file) or SLACK_API_URL))
    telegram = env_value("TELEGRAM_BOT_TOKEN", env_file)
    if telegram:
        found.append(
            TelegramChannel(telegram, env_value("TELEGRAM_API_URL", env_file) or TELEGRAM_API_URL)
        )
    return found


def deliver(notification: Notification, team: Team | None, channels: Sequence[Channel]) -> str:
    """Send one notification on every channel; returns how it went, for the store."""
    logger = get_logger("incidents")
    logger.info("%s %s", notification.sent_at.isoformat(sep=" "), notification.message())
    if not channels:
        return "local"
    if team is None:
        return "local: no team " + notification.team
    outcome = []
    for channel in channels:
        try:
            sent = channel.send(notification, team)
            outcome.append(channel.name if sent else f"{channel.name}: no address")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as error:
            logger.warning("%s for %s failed: %s", channel.name, notification.incident_id, error)
            outcome.append(f"{channel.name} failed")
    return ", ".join(outcome)
