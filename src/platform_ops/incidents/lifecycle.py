"""When a simulated owner acknowledges and resolves an incident.

Nobody really answers the pages, so the response is modelled. Each severity has
a base time to acknowledge and a base time to fix. Both stretch with how many
incidents the owner already has open, because a person juggling three fires is
slower on the fourth, and both get a spread between 0.5x and 1.5x drawn from a
stable hash of the seed and incident, so two runs agree to the second.

This is the part of the scenario that turns ownership concentration into a
number: when every source belongs to one team, that team's queue is what makes
MTTR grow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from platform_ops.common.config import LifecycleSettings, Severity
from platform_ops.simulation.workload import stable_hash

_HASH_SPACE = 2**64


def _spread(seed: int, incident_id: str, step: str) -> float:
    """A factor in [0.5, 1.5), the same for the same inputs everywhere."""
    return 0.5 + stable_hash(seed, incident_id, step) / _HASH_SPACE


@dataclass(frozen=True)
class Response:
    acknowledged_at: datetime
    resolved_at: datetime


def respond(
    incident_id: str,
    severity: Severity,
    opened_at: datetime,
    owner_open_incidents: int,
    settings: LifecycleSettings,
    seed: int,
) -> Response:
    """Acknowledgement and resolution times for a newly opened incident.

    ``owner_open_incidents`` counts the owner's other incidents still open at
    ``opened_at``, not this one.
    """
    load = 1 + settings.load_factor * owner_open_incidents
    ack_minutes = settings.ack_minutes[severity] * load * _spread(seed, incident_id, "ack")
    fix_hours = settings.resolve_hours[severity] * load * _spread(seed, incident_id, "resolve")
    acknowledged_at = opened_at + timedelta(seconds=round(ack_minutes * 60))
    resolved_at = acknowledged_at + timedelta(seconds=round(fix_hours * 3600))
    return Response(acknowledged_at, resolved_at)
