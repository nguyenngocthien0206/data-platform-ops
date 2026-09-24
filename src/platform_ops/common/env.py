"""Reading ``.env`` for local service credentials.

Docker Compose reads ``.env`` for the legacy databases' passwords and ports,
and the toolkit reads the same file, so there is one place to set them. A real
environment variable always wins over the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def read_dotenv(path: Path) -> dict[str, str]:
    """``KEY=value`` lines from ``path``; comments and blank lines are skipped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        if sep:
            values[name.strip()] = value.strip().strip("'\"")
    return values


def env_value(name: str, env_file: Path, default: str | None = None) -> str | None:
    """``name`` from the environment, else from ``env_file``, else ``default``."""
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env
    return read_dotenv(env_file).get(name) or default
