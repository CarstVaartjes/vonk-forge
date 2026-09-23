"""Exact, human-friendly selector handling for the operator CLI."""

from __future__ import annotations

from collections.abc import Sequence


class SelectorError(ValueError):
    """An operator supplied a selector that is missing or not unique."""

    def __init__(self, message: str, *, candidates: Sequence[str] = ()) -> None:
        self.candidates = tuple(candidates)
        super().__init__(message)
