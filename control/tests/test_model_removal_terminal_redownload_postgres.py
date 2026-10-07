"""A completed crash-recovered removal cannot poison a fresh exact download."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from vonk_control.model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheRemovalPayload,
    parse_model_cache_payload,
)
from vonk_control.models import (
    ArtifactLifecycleGate,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)

from .test_model_cache import _artifact
from .test_model_removal_reference_lifecycle import _removal_service
from .test_model_removal_worker_postgres import (
    test_model_removal_process_death_after_unlink_reuses_pending_byte_checkpoint as _crash_unlink_and_recover,
)


def test_terminal_crash_recovered_removal_allows_new_exact_download(
    postgres_engine, tmp_path: Path
) -> None:
    # Reuse the actual child-process death guard: it exits after filesystem
    # unlink, then a new owner consumes the original pending-byte checkpoint.
    _crash_unlink_and_recover(postgres_engine, tmp_path)
    service, sessions = _removal_service(postgres_engine, tmp_path)
    data = bytes(range(128)) * 64
    object_digest = hashlib.sha256(data).hexdigest()
    with sessions() as session:
        removed = session.scalar(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        )
        assert removed is not None and removed.state == "succeeded"
        original_id, original_key = removed.id, removed.request_key
        terminal_bytes = copy.deepcopy(removed.payload)
        removal = parse_model_cache_payload("remove", removed.payload)
        assert isinstance(removal, ModelCacheRemovalPayload)
        assert removal.model_content_sha256 is not None
        assert removal.result is not None
        assert removal.result.reclaimed_bytes == len(data)
        assert removal.selected_objects == [object_digest]
        assert removal.result.removed_entries == removal.selected
        assert len(removal.selected) == 1
        removed_set = removal.selected[0]
        assert session.get(ModelCacheSet, removed_set) is None
        assert not tuple(session.scalars(select(ModelCacheSetArtifact)))
        assert not tuple(
            session.scalars(
                select(ArtifactLifecycleGate).where(
                    ArtifactLifecycleGate.removal_owner_id == original_id
                )
            )
        )
    object_path = service._object_path(object_digest)
    receipt_path = service._receipt_path(object_digest)
    assert not object_path.exists() and not receipt_path.exists()
    # This is a fixture transport of the identical reviewed asset manifest,
    # not replacement cache bytes or a synthetic successful receipt.
    artifact = _artifact(
        tmp_path / "source-removal-process-death",
        data,
        model_content_sha256=removal.model_content_sha256,
    )
    preview = service.download_preview(
        model_content_sha256=removal.model_content_sha256, artifacts=[artifact]
    )
    assert preview["blockers"] == []
    fresh_key = str(uuid4())
    try:
        accepted = service.start_download(
            actor="operator",
            request_key=fresh_key,
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=removal.model_content_sha256,
            artifacts=[artifact],
            selector=removal.selector,
        )
        assert accepted.id != original_id and accepted.request_key != original_key
        assert accepted.state == "queued"
        claimed = service._claim_operations(limit=1, respect_backoff=False)
        assert claimed == [(accepted.id, "download")]
        with sessions() as session:
            fresh = session.get(ModelCacheOperation, accepted.id)
            assert fresh is not None and fresh.state == "running"
            assert fresh.request_key == fresh_key and fresh.fence is not None
            payload = parse_model_cache_payload("download", fresh.payload)
            assert isinstance(payload, ModelCacheDownloadPayload)
            assert payload.artifact_set_sha256 == removed_set
        service._run_download(accepted.id, force=False)
        completed = service.get_operation(accepted.id)
        assert completed.state == "succeeded", completed.last_error
        assert object_path.read_bytes() == data and receipt_path.is_file()
        with sessions() as session:
            restored = session.get(ModelCacheSet, removed_set)
            assert restored is not None and restored.state == "cached"
            assert restored.verified_bytes == len(data)
            memberships = tuple(session.scalars(select(ModelCacheSetArtifact)))
            assert len(memberships) == 1
            assert memberships[0].artifact_set_sha256 == removed_set
            assert memberships[0].artifact_sha256 == object_digest
            old = session.get(ModelCacheOperation, original_id)
            assert old is not None and old.state == "succeeded"
            assert old.request_key == original_key and old.payload == terminal_bytes
            assert not tuple(
                session.scalars(
                    select(ArtifactLifecycleGate).where(
                        ArtifactLifecycleGate.removal_owner_id == original_id
                    )
                )
            )
        assert service.advance_removals(limit=1) == 0
        assert object_path.read_bytes() == data
    finally:
        service.close()
