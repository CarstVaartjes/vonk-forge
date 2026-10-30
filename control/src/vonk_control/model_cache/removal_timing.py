"""Per-phase wall time of a removal review or acceptance, logged once per request."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

from ..logging import log_event
from .constants import _LOGGER

REMOVAL_REVIEW_EVENT = "model_cache.removal_review"


class RemovalPhase(StrEnum):
    RESOLVE = "resolve"
    SCOPE = "scope"
    REFERENCES = "references"
    OWNERS = "owners"
    ASSETS = "assets"
    EVIDENCE = "evidence"
    MODEL_OBJECTS = "model_objects"
    INSTALLATIONS = "installations"
    NODES = "nodes"
    ACCEPT_VERIFY = "accept_verify"


_ACTIVE: ContextVar[dict[RemovalPhase, float] | None] = ContextVar(
    "removal_review_phases", default=None
)


@contextmanager
def removal_timing(selector: str, *, fleet: bool) -> Iterator[None]:
    """Collect phase times for this request; the outermost caller logs them.

    A fleet review runs the NAS review inside it, so only one event per request
    is written, with every phase it spent time in.
    """
    if _ACTIVE.get() is not None:
        yield
        return
    phases: dict[RemovalPhase, float] = {}
    token = _ACTIVE.set(phases)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        log_event(
            _LOGGER,
            REMOVAL_REVIEW_EVENT,
            service="controller",
            selector=selector,
            fleet=fleet,
            **{
                phase.value: round(phases.get(phase, 0.0) * 1000)
                for phase in RemovalPhase
            },
        )


@contextmanager
def removal_phase(phase: RemovalPhase) -> Iterator[None]:
    """Add the wall time of the block to ``phase`` of the current request."""
    phases = _ACTIVE.get()
    if phases is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        phases[phase] = phases.get(phase, 0.0) + time.perf_counter() - started
