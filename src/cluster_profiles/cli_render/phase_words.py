"""The one place that turns a phase or a wait into plain words for a person.

The follow view, the one-shot view and every progress line use these; the words
themselves come from the generated vocabulary, shared with the web.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cluster_profiles.cli_states_generated import (
    PHASE_WORDS,
    WAIT_DEPENDENCY_PENDING,
    WAIT_SPARK_BUSY,
    WAIT_WORDS,
)


def phase_words(phase: str) -> str:
    """A phase in plain words; a phase this table does not know shows as sent."""
    return PHASE_WORDS.get(phase, phase)


@dataclass(frozen=True)
class Meter:
    """One measured byte meter exactly as the Controller reports it."""

    phase: str
    completed: int | None
    total: int | None
    rate: float | None
    eta_seconds: float | None
    label: str | None = None
    member_id: str | None = None
    activity: str | None = None
    items: tuple[object, object] | None = None


@dataclass(frozen=True)
class Measured:
    """What an observation says: the overall meter and one meter per Spark."""

    overall: Meter
    members: tuple[Meter, ...]

    @property
    def phase(self) -> str:
        return self.overall.phase


def measured(observed: object) -> Measured:
    """Read phase, bytes, rate, ETA and members as the Controller serves them.

    An application carries its running child's meter under
    ``progress.child_progress.operation``; an operation carries it under
    ``progress`` or ``progress.operation``.  Rate and ETA are the Controller's
    own (``smoothed_bytes_per_second``, ``eta_seconds``); nothing is derived here.
    """
    root = _mapping(observed)
    outer = _mapping(root.get("progress"))
    progress = outer
    nested = outer.get("operation")
    if not isinstance(nested, Mapping):
        child = _mapping(outer.get("child_progress"))
        nested = child.get("operation", child) if child else None
    if isinstance(nested, Mapping) and nested:
        progress = nested
    phase = progress.get(
        "phase", outer.get("phase", root.get("phase", root.get("state")))
    )
    label = progress.get("current_label", outer.get("current_label"))
    members = progress.get("members")
    return Measured(
        _meter(progress, "working" if phase is None else str(phase), label),
        tuple(
            _meter(item, str(item.get("phase", phase)), None)
            for item in (members if isinstance(members, list) else [])
            if isinstance(item, Mapping)
        ),
    )


def _meter(source: object, phase: str, label: object) -> Meter:
    source = _mapping(source)
    completed = source.get("completed_bytes")
    total = source.get("total_bytes")
    rate = source.get("smoothed_bytes_per_second")
    eta = source.get("eta_seconds")
    member = source.get("member_id")
    return Meter(
        phase,
        completed if type(completed) is int else None,
        total
        if type(total) is int and source.get("total_bytes_known") is not False
        else None,
        float(rate) if isinstance(rate, int | float) and rate > 0 else None,
        float(eta) if isinstance(eta, int | float) and eta >= 0 else None,
        None if label is None else str(label),
        member if isinstance(member, str) else None,
        source.get("activity") if isinstance(source.get("activity"), str) else None,
        (source["completed_items"], source.get("total_items"))
        if source.get("completed_items") is not None
        else None,
    )


def wait_words(observed: object, blocker: object) -> str:
    """Why work waits, in plain words: the step for a dependency, else the code."""
    code = _mapping(blocker).get("code")
    if code == WAIT_DEPENDENCY_PENDING:
        phase = measured(observed).phase
        text = phase_words(phase)
        return text[0].lower() + text[1:]
    if code == WAIT_SPARK_BUSY:
        return WAIT_WORDS[WAIT_SPARK_BUSY]
    return str(code)


def _mapping(value: object):
    return value if isinstance(value, Mapping) else {}
