"""The ownership registry: CODEOWNERS for data.

``config/teams.yaml`` says who is on which team. ``config/ownership.yaml`` maps
glob patterns on dbt unique ids to an owner, a team and a tier.

Resolution rule (ADR 0002): every pattern that matches a unique id is a
candidate. The most specific candidate wins, where specificity is

1. more literal (non-wildcard) characters, then
2. fewer wildcard tokens (``*``, ``?``, ``[...]``).

If the two best candidates are still equal, the dataset's ownership is
ambiguous and :func:`platform_ops.metadata.check.run_check` fails, naming both
rules. Falling back to file order would let a reordering of lines silently hand
a dataset to another team, which is the drift this registry exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from platform_ops.common.config import Tier


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Team(_Strict):
    id: str
    name: str
    channel: str
    members: Annotated[list[str], Field(min_length=1)]


class TeamsFile(_Strict):
    teams: list[Team]

    @model_validator(mode="after")
    def _unique_ids(self) -> TeamsFile:
        ids = [team.id for team in self.teams]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicate team ids: {', '.join(duplicated)}")
        return self


class DatasetRule(_Strict):
    match: str
    owner: str
    team: str
    tier: Tier


class OwnershipFile(_Strict):
    datasets: list[DatasetRule]

    @model_validator(mode="after")
    def _unique_patterns(self) -> OwnershipFile:
        patterns = [rule.match for rule in self.datasets]
        duplicated = sorted({p for p in patterns if patterns.count(p) > 1})
        if duplicated:
            raise ValueError(f"duplicate ownership patterns: {', '.join(duplicated)}")
        return self


def specificity(pattern: str) -> tuple[int, int]:
    """``(literal characters, wildcard tokens)`` of an fnmatch pattern."""
    literal = 0
    wildcards = 0
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char in "*?":
            wildcards += 1
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 2)  # a ']' right after '[' is literal
            if close == -1:
                literal += 1  # fnmatch treats an unclosed '[' as a literal
                i += 1
            else:
                wildcards += 1
                i = close + 1
        else:
            literal += 1
            i += 1
    return literal, wildcards


def _rank(rule: DatasetRule) -> tuple[int, int]:
    literal, wildcards = specificity(rule.match)
    return (-literal, wildcards)


@dataclass(frozen=True)
class Resolution:
    unique_id: str
    rule: DatasetRule | None
    candidates: tuple[DatasetRule, ...]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) >= 2 and _rank(self.candidates[0]) == _rank(self.candidates[1])

    @property
    def owned(self) -> bool:
        return self.rule is not None and not self.ambiguous


@dataclass(frozen=True)
class Registry:
    teams: dict[str, Team]
    rules: tuple[DatasetRule, ...]

    @classmethod
    def load(cls, teams_path: Path, ownership_path: Path) -> Registry:
        teams_file = TeamsFile.model_validate(_read_yaml(teams_path))
        ownership = OwnershipFile.model_validate(_read_yaml(ownership_path))
        return cls(
            teams={team.id: team for team in teams_file.teams},
            rules=tuple(ownership.datasets),
        )

    @classmethod
    def from_config_dir(cls, config_dir: Path) -> Registry:
        return cls.load(config_dir / "teams.yaml", config_dir / "ownership.yaml")

    def problems(self) -> list[str]:
        """Rules that point at a team that does not exist, or an owner not on it."""
        found: list[str] = []
        for rule in self.rules:
            team = self.teams.get(rule.team)
            if team is None:
                found.append(f"rule '{rule.match}' names unknown team '{rule.team}'")
            elif rule.owner not in team.members:
                found.append(
                    f"rule '{rule.match}' names owner '{rule.owner}', "
                    f"who is not a member of team '{rule.team}'"
                )
        return found

    def team_of(self, person: str) -> str | None:
        """The team a person belongs to, used later to attribute ad hoc queries."""
        for team in self.teams.values():
            if person in team.members:
                return team.id
        return None

    def resolve(self, unique_id: str) -> Resolution:
        candidates = tuple(
            sorted(
                (rule for rule in self.rules if fnmatchcase(unique_id, rule.match)),
                key=lambda rule: (*_rank(rule), rule.match),
            )
        )
        return Resolution(
            unique_id=unique_id,
            rule=candidates[0] if candidates else None,
            candidates=candidates,
        )


def _read_yaml(path: Path) -> object:
    if not path.is_file():
        raise FileNotFoundError(f"No registry file at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
