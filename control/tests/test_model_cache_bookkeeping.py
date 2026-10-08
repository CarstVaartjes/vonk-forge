"""Bookkeeping mismatches reconcile; damaged state rebuilds or retires.

Rule 5 of the lifecycle core (see ``lifecycle/evidence.py``): a persisted
document that does not read, or a stored fact that no longer matches what is
observed, is *unknown*, never a reason to stop a load.  Three families:

1. a mismatch reconciles instead of failing (a vanished operation or set row);
2. damaged persisted state is rebuilt from evidence, else retired, and the loop
   carries on with the other rows;
3. the security and input-validation refusals still refuse.
"""

# ruff: noqa: F811 - tests take the imported ``cache`` fixture by name
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import (
    ModelCacheConflict,
    ModelCacheNotFound,
    ModelCacheResolutionError,
    _validate_source,
)
from vonk_control.models import (
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)

from .non_blocking import assert_ended_without_blocking
from .test_model_cache import (
    _artifact,
    _download,
    cache,  # noqa: F401 - the fixture
)
from .test_model_cache_lifecycle import _edit, _queue

MODEL = "a" * 64


def _set_row(sessions, digest: str) -> ModelCacheSet:
    with sessions() as session:
        row = session.get(ModelCacheSet, digest)
        assert row is not None
        session.expunge(row)
        return row


# ------------------------------------------------- 1. a mismatch reconciles


def test_a_vanished_operation_is_skipped_not_raised(cache):
    service, _sessions = cache
    missing = "00000000-0000-4000-8000-00000000b001"
    assert service._start_transfer(missing, force=False) is None
    service._run_download(missing, force=False)  # returns: nothing left to run
    service._schedule_background_download(missing, force=False, capacity=1)
    service._set_operation_progress(
        missing,
        None,  # type: ignore[arg-type] - never reached for a vanished operation
        phase="downloading",
        completed_artifacts=0,
        downloaded_bytes=0,
        current_artifact_key=None,
    )
    assert service._operation_transfer_snapshot(missing) == (None, 0)
    assert service._try_settle_cancellation(missing) is True


def test_a_missing_set_column_is_rederived_from_the_manifest(cache, tmp_path: Path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000b002"
    )
    expected = operation.artifact_set_sha256
    assert expected is not None
    _edit(sessions, operation.id, artifact_set_sha256=None)
    service.run_pending()  # used to raise ``set_missing`` and stop the batch
    finished = service.get_operation(operation.id)
    assert finished.state == "succeeded", finished.last_error
    assert finished.artifact_set_sha256 == expected


def test_a_stale_removal_scope_waits_instead_of_raising(cache, tmp_path: Path):
    """A set that references a removal target after the plan was made defers."""

    service, sessions = cache
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"shared bytes")],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000b003",
    )
    set_digest = str(downloaded.artifact_set_sha256)
    with sessions.begin() as session:
        accepted = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key="00000000-0000-4000-8000-00000000b004",
            selector="recipe-child",
            selected_sets=[set_digest],
        )
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, accepted.id)
        assert row is not None
        payload = dict(row.payload)
        object_digest = str(payload["delete_objects"][0])
        # Every target is reconciled; then another set starts to reference it.
        payload["object_index"] = len(payload["delete_objects"])
        payload["set_index"] = len(payload["selected"])
        payload["object_pending_bytes"] = None
        row.payload = payload
        session.add(
            ModelCacheSetArtifact(
                artifact_set_sha256="b" * 64,
                artifact_key="late",
                artifact_sha256=object_digest,
                path="late.bin",
            )
        )
    assert service._advance_model_removal(accepted.id) is False  # no raise
    waiting = service.get_operation(accepted.id)
    assert waiting.state in {"queued", LifecycleState.BACKOFF}
    assert waiting.failure is not None and waiting.failure["retryable"] is True
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest) is not None  # still fenced


# ------------------------------- 2. damaged persisted state rebuilds or retires


@pytest.mark.usefixtures("damaged_json_rows")
def test_one_unreadable_download_never_stops_the_claim_loop(cache, tmp_path: Path):
    service, sessions = cache
    broken, _ = _queue(service, tmp_path, "00000000-0000-4000-8000-00000000b010", b"a")
    healthy, _ = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000b011", b"bb"
    )
    with sessions.begin() as session:
        # Nothing re-derives it: the payload is junk and the set row is gone.
        stored = session.get(ModelCacheOperation, broken.id)
        assert stored is not None
        stored.payload = {"unrelated": True}
        stored.artifact_set_sha256 = None
        for row in session.query(ModelCacheSet).filter(
            ModelCacheSet.artifact_set_sha256 == broken.artifact_set_sha256
        ):
            session.delete(row)
    claimed = service._claim_operations(limit=5, respect_backoff=False)
    assert [operation_id for operation_id, _kind in claimed] == [healthy.id]
    retired = service.get_operation(broken.id)  # the view reads without raising
    assert "persisted-state-damaged" in str(retired.last_error)
    assert retired.failure is not None

    def reason(receipt):
        assert receipt.failure is not None and receipt.failure["code"]

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        retired,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _queue(service, tmp_path, str(uuid.uuid4()), b"a")[0],
        assert_reason=reason,
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_running_operation_under_a_live_lease_is_never_retired(cache, tmp_path: Path):
    """Fail open: another process's running workload survives an unreadable document."""

    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000b013"
    )
    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        stored.state = "running"
        stored.fence = "another-process"
        stored.lease_deadline = datetime.now(UTC) + timedelta(seconds=60)
        stored.payload = {"written_by": "a newer Controller"}
    assert service._claim_operations(limit=5, respect_backoff=False) == []
    with sessions() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        assert stored.state == "running" and stored.fence == "another-process"


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_envelope_is_rebuilt_from_its_set_row(cache, tmp_path: Path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000b012"
    )
    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        payload = dict(stored.payload)
        payload["manifest"] = {"artifacts": "damaged"}
        payload.pop("retry")
        stored.payload = payload
    claimed = service._claim_operations(limit=1, respect_backoff=False)
    assert [operation_id for operation_id, _kind in claimed] == [operation.id]
    service._run_download(operation.id, force=False)
    assert service.get_operation(operation.id).state == "succeeded"


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_set_manifest_does_not_block_reconcile_or_listing(
    cache, tmp_path: Path
):
    service, sessions = cache
    good = _download(
        service,
        [_artifact(tmp_path, b"good bytes")],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000b020",
    )
    other = _download(
        service,
        [_artifact(tmp_path, b"other bytes", artifact_id="other", path="o.bin")],
        model_content_sha256="c" * 64,
        request_key="00000000-0000-4000-8000-00000000b021",
    )
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, other.artifact_set_sha256)
        assert row is not None
        row.manifest = {"damaged": True}
    service.reconcile_storage()  # used to raise on the first damaged row
    listing = service.inventory()
    by_set = {entry["artifact_set_sha256"]: entry for entry in listing["entries"]}
    assert by_set[good.artifact_set_sha256]["artifacts"]
    assert by_set[other.artifact_set_sha256]["artifacts"] == []  # unknown, listed
    assert service.get_entry(str(good.artifact_set_sha256))["state"] == "cached"
    with pytest.raises(ModelCacheNotFound):  # unreadable and not re-derivable
        service.manifest_for_artifact_set(str(other.artifact_set_sha256))


# --------------------------------- 3. security and input refusals still refuse


@pytest.mark.parametrize(
    "source",
    [
        "https://user:secret@huggingface.co/a/b",  # credentials in the URL
        "ftp://huggingface.co/a/b",  # not HTTPS, HTTP or file
        "https://huggingface.co:8443/a/b",  # an explicit port
    ],
)
def test_untrusted_sources_are_still_refused(source: str):
    with pytest.raises(ModelCacheResolutionError) as refused:
        _validate_source(source)
    assert refused.value.code == "model_cache.source_invalid"


def test_unsafe_artifact_paths_are_still_refused(cache, tmp_path: Path):
    service, _sessions = cache
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"served bytes")],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000b030",
    )
    set_digest = str(downloaded.artifact_set_sha256)
    manifest = service.manifest_for_artifact_set(set_digest)
    object_digest = manifest.artifacts[0].sha256
    path, size, _digest = service.cached_artifact_file(
        set_digest, object_digest, "weights.bin"
    )
    assert path.is_file() and size == len(b"served bytes")
    for unsafe in ("../weights.bin", "/etc/passwd"):
        with pytest.raises(ModelCacheNotFound) as refused:
            service.cached_artifact_file(set_digest, object_digest, unsafe)
        assert refused.value.code == "model_cache.artifact_missing"
    from vonk_agent_protocol import UnknownOutcomeError

    with pytest.raises(UnknownOutcomeError):
        service.cached_artifact_file(set_digest, object_digest, "other.bin")
    assert (
        service.cached_artifact_file(set_digest, object_digest, "weights.bin")[
            0
        ].read_bytes()
        == b"served bytes"
    )


def test_requests_naming_nothing_still_get_a_defined_refusal(cache, tmp_path):
    service, sessions = cache
    unknown = "00000000-0000-4000-8000-00000000b040"
    with pytest.raises(ModelCacheNotFound) as missing_operation:
        service.get_operation(unknown)
    assert missing_operation.value.code == "model_cache.operation_missing"
    with pytest.raises(ModelCacheNotFound) as missing_entry:
        service.get_entry("d" * 64)
    assert missing_entry.value.code == "model_cache.entry_missing"
    with pytest.raises(ModelCacheNotFound):
        service.retry(unknown, actor="test", request_key=unknown[:-1] + "1")
    with pytest.raises(ModelCacheConflict) as malformed:
        service.cancel_operation(
            unknown, actor="test", request_key="bad", reason="not a uuid"
        )
    assert malformed.value.code == "model_cache.cancellation_invalid"

    accepted, _artifact_document = _queue(service, tmp_path, str(uuid.uuid4()))
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=lambda receipt: service.cancel_operation(
            receipt.id,
            actor="test",
            request_key=str(uuid.uuid4()),
            reason="valid request after refusals",
        ),
        fresh=lambda _world: _queue(service, tmp_path, str(uuid.uuid4()))[0],
    )
