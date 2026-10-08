"""Withdrawal publication retries uncertainty without another operator request."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from pathlib import Path

import pytest
from vonk_agent_protocol import WaitReason
from vonk_control import route_runtime
from vonk_control.recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteNotReady
from vonk_control.recipe_routes import publisher as publisher_module
from vonk_control.route_runtime import ActivationMarker, AtomicRouteBundlePublisher

from .test_route_runtime import _verified_bundle


@pytest.mark.parametrize("clears", [True, False])
def test_empty_publication_retries_lock_contention_and_bounds_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clears: bool
) -> None:
    runtime = AtomicRouteBundlePublisher(tmp_path)
    publisher = AtomicRecipeRoutePublisher(runtime)
    monkeypatch.setattr(route_runtime, "_PUBLICATION_LOCK_BUDGET_SECONDS", 0)
    attempts: list[int] = []
    with (tmp_path / ".publication.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def retry_schedule() -> Iterator[int]:
            for attempt in range(3):
                if attempt == 1 and clears:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                attempts.append(attempt)
                yield attempt

        monkeypatch.setattr(publisher_module, "bounded_attempts", retry_schedule)
        if clears:
            generation = publisher.publish_empty("a" * 64)
            assert attempts == [0, 1]
            assert _verified_bundle(tmp_path).marker.generation == generation.generation
        else:
            with pytest.raises(RecipeRouteNotReady) as caught:
                publisher.publish_empty("a" * 64)
            assert caught.value.typed_reason == WaitReason.OBSERVATION_UNAVAILABLE
            assert attempts == [0, 1, 2]
            assert not (tmp_path / "activation.json").exists()
            fcntl.flock(lock, fcntl.LOCK_UN)
            # Exhaustion cannot poison a fresh authorized publication.
            publisher.publish_empty("a" * 64)
            assert _verified_bundle(tmp_path).marker.generation == 1


def test_busy_retry_after_lost_ack_keeps_the_activated_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acknowledged: list[int] = []

    def acknowledge(marker: ActivationMarker) -> None:
        acknowledged.append(marker.generation)
        if len(acknowledged) == 1:
            raise OSError("lost supervisor acknowledgement")

    runtime = AtomicRouteBundlePublisher(tmp_path, await_supervisor_ack=acknowledge)
    publisher = AtomicRecipeRoutePublisher(runtime)
    monkeypatch.setattr(route_runtime, "_PUBLICATION_LOCK_BUDGET_SECONDS", 0)
    with (tmp_path / ".publication.lock").open("w") as lock:

        def retry_schedule() -> Iterator[int]:
            yield 0
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield 1
            fcntl.flock(lock, fcntl.LOCK_UN)
            yield 2

        monkeypatch.setattr(publisher_module, "bounded_attempts", retry_schedule)
        generation = publisher.publish_empty("b" * 64)
    assert acknowledged == [generation.generation, generation.generation]
    assert len(list((tmp_path / "generations").iterdir())) == 1
