"""The state every added-line mode block reads and appends to."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class CheckContext:
    path: str
    source: str
    added: set[int]
    modes: Sequence[str]
    words: frozenset[str] | None
    removed: Sequence[str]
    is_test: bool
    python: bool
    hits: list[tuple[int, str]] = field(default_factory=list)
    noexit_hits: list[str] = field(default_factory=list)
