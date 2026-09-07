"""Catalog bootstrap boundary for the canonical recipe catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SeedResult:
    created: int
    identifiers: tuple[str, ...]


def seed_builtin_harnesses(session: Session, now: datetime) -> SeedResult:
    """Do not seed retired execution-harness catalog entities on bootstrap."""

    del session
    if now.tzinfo is None:
        raise ValueError("catalog seed timestamp must be timezone-aware")
    return SeedResult(created=0, identifiers=())
