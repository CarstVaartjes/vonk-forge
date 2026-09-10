"""Exact, human-friendly selector handling for the operator CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class SelectorError(ValueError):
    """An operator supplied a selector that is missing or not unique."""

    def __init__(self, message: str, *, candidates: Sequence[str] = ()) -> None:
        self.candidates = tuple(candidates)
        super().__init__(message)


def selector_for(item: Mapping[str, object]) -> str | None:
    """Return the canonical readable selector emitted by a Controller row."""
    for key in ("selector", "name", "display_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def select_exact(
    items: Sequence[Mapping[str, object]], requested: str, *, noun: str
) -> Mapping[str, object]:
    """Resolve an exact selector or unique displayed name.

    Prefix and fuzzy matches are intentionally rejected for mutations. This
    keeps a shell script from silently choosing a different model variant.
    """
    needle = requested.strip().casefold()
    if not needle:
        raise SelectorError(f"{noun} selector cannot be empty")
    matches: list[Mapping[str, object]] = []
    for item in items:
        values = [
            value
            for key in ("selector", "name", "display_name")
            if isinstance(value := item.get(key), str)
        ]
        if any(value.casefold() == needle for value in values):
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    candidates = tuple(
        selector
        for item in matches
        if (selector := selector_for(item)) is not None
    )
    if not matches:
        raise SelectorError(f"unknown {noun} selector: {requested}")
    raise SelectorError(
        f"ambiguous {noun} selector: {requested}; choose one of {', '.join(candidates)}",
        candidates=candidates,
    )


def selectors_from_payload(payload: Mapping[str, object], noun: str) -> list[Mapping[str, object]]:
    """Extract rows from the documented overview/list envelopes."""
    keys = {
        "fleet": ("nodes", "sparks", "fleet"),
        "model": ("models", "entries", "items"),
        "recipe": ("recipes", "entries", "items"),
        "profile": ("profiles", "items"),
    }[noun]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []
