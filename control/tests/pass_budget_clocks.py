"""Freeze pass budgets, never the process-wide clock or hang/lock deadlines.

All Controller tests inherit this fixture, including callers reached through
shared worlds, service fixtures and HTTP routes. Budget tests replace a module's
clock themselves, or advance this clock at the effect they are measuring.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from concurrent.futures import ALL_COMPLETED, Future, wait
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace

import pytest

# Lookup owners, including split modules that consume the same pass deadline.
# TTLs, lock waits, admission retries and transfer-stall deadlines are separate:
# freezing those would prevent recovery or make a blocked wait infinite.
PASS_BUDGET_MODULES = (
    "vonk_control.due_waits",
    "vonk_control.terminal_history_collection",
    "vonk_control.catalog_revision_collection",
    "vonk_control.unused_storage_collection.service",
    "vonk_control.unused_storage_collection.pressure",
    "vonk_control.fleet_profile_adapter_conversion",
    "vonk_control.recipe_image_availability.removal_acceptance",
    "vonk_control.recipe_image_availability.removal_execution",
    "vonk_control.library_projection.views",
    "vonk_control.library_projection.local_state",
    "vonk_control.library_assessment",
    "vonk_control.library_image_presence",
    "vonk_control.model_cache.updates",
    "vonk_control.profile_read_observation",
)


@dataclass
class PassClock:
    seconds: float = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class CompletingThread(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        # Ordinary assertions wait for the work, not the production latency
        # budget. pytest's independent hang guard still bounds broken workers.
        super().join()


def completing_wait[Value](
    futures: Collection[Future[Value]],
    *,
    timeout: float,
    return_when: str = ALL_COMPLETED,
) -> tuple[set[Future[Value]], set[Future[Value]]]:
    return wait(futures, return_when=return_when)


type Wait = Callable[..., tuple[set[Future[object]], set[Future[object]]]]


def expiring_wait(clock: PassClock, entered: threading.Event) -> Wait:
    """Expire the budget only once real work has reached the injected fault."""

    def expired(
        futures: Collection[Future[object]],
        *,
        timeout: float,
        return_when: str = ALL_COMPLETED,
    ) -> tuple[set[Future[object]], set[Future[object]]]:
        assert entered.wait(timeout=30), "the work never reached the injected fault"
        clock.advance(timeout)
        return wait(futures, timeout=0, return_when=return_when)

    return expired


@pytest.fixture(autouse=True)
def frozen_pass_budgets(monkeypatch: pytest.MonkeyPatch) -> PassClock:
    """Runner load cannot consume a pass's budget before its first effect."""
    clock = PassClock()
    for module_name in PASS_BUDGET_MODULES:
        monkeypatch.setattr(import_module(module_name), "time", clock)
    monkeypatch.setattr(
        "vonk_control.library_assessment.threading",
        SimpleNamespace(
            Thread=CompletingThread, Lock=threading.Lock, Event=threading.Event
        ),
    )
    for owner in (
        "vonk_control.library_image_presence",
        "vonk_control.model_cache.updates",
        "vonk_control.profile_read_observation",
    ):
        monkeypatch.setattr(f"{owner}.wait", completing_wait)
    # Completed probes stay fresh until this clock advances; never across tests.
    monkeypatch.setattr("vonk_control.profile_read_observation._RECENT", OrderedDict())
    return clock
