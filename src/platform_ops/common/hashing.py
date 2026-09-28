"""The one source of anything that looks random.

Two runs of ``make demo`` must write identical reports, so nothing may depend
on Python's per-process ``hash()`` salt or on the wall clock (ADR 0004). Every
module that needs a repeatable pseudo-random choice asks this function.
"""

from __future__ import annotations

import hashlib


def stable_hash(*parts: object) -> int:
    """A 64-bit hash that is the same in every process and on every machine."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")
