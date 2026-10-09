"""A failed unlock must close its kernel fence before fresh work is admitted."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest
from pydantic import BaseModel
from vonk_agent_protocol import LifecycleState, WaitReason
from vonk_control.artifact_blob_store import ArtifactBlobStore

from .non_blocking import assert_ended_without_blocking


class Receipt(BaseModel):
    request_key: str
    state: LifecycleState
    reason_code: WaitReason | None = None


def test_unlock_failure_releases_fence_and_admits_fresh_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch leaking the descriptor, which holds the fence after LOCK_UN fails."""
    store = ArtifactBlobStore(tmp_path / "blobs")
    original = fcntl.flock
    fail_unlock = True

    def flock(descriptor: int, mode: int) -> None:
        nonlocal fail_unlock
        if mode == fcntl.LOCK_UN and fail_unlock:
            fail_unlock = False
            raise OSError("injected unlock failure")
        original(descriptor, mode)

    monkeypatch.setattr(fcntl, "flock", flock)

    def end(_receipt: Receipt) -> Receipt:
        observed_error = None
        try:
            with store.reference_reconciliation():
                pass
        except Exception as error:  # noqa: BLE001 - any interrupted effect
            observed_error = error
        else:
            pytest.fail("fault injection did not interrupt unlock")
        assert observed_error is not None
        return Receipt(
            request_key="original",
            state=LifecycleState.FAILED,
            reason_code=WaitReason.OBSERVATION_UNAVAILABLE,
        )

    def released() -> None:
        # A different descriptor sees the actual kernel ownership, rather than
        # trusting an in-memory flag. Do not wait for the production budget.
        with (tmp_path / "blobs" / ".references.lock").open("rb") as stream:
            original(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            original(stream.fileno(), fcntl.LOCK_UN)

    def fresh(owner: ArtifactBlobStore) -> Receipt:
        with owner.reference_attachment():
            return Receipt(request_key="fresh", state=LifecycleState.RUNNING)

    assert_ended_without_blocking(
        store,
        Receipt(request_key="original", state=LifecycleState.RUNNING),
        end=end,
        fresh=fresh,
        assert_released=released,
    )
