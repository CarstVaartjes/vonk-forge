"""PostgreSQL retry behavior when model-removal finalization cannot lock gates."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import ArtifactLifecycleGate
from vonk_control.model_cache import CacheOperationView, ModelCacheService
from vonk_control.model_cache_contract import ModelCacheRemovalResult
from vonk_control.models import ModelCacheSet
from vonk_forge_contracts import ModelDefinition, content_sha256

from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_reference_lifecycle import (
    _register_model,
    _removal_service,
    _seed_model,
    _settle_removal,
)


def _seed_unrelated_model(
    service: ModelCacheService,
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> tuple[str, str, str]:
    data = b"independent model bytes"
    model: ModelDefinition = _canonical_model(
        publisher="vonk-forge",
        slug="finalization-contention-unrelated",
        file_id="weights",
        file_digest=hashlib.sha256(data).hexdigest(),
    )
    selector = _register_model(sessions, model)
    digest = content_sha256(model)
    artifact = _artifact(tmp_path, data, model_content_sha256=digest)
    operation = _download(
        service,
        [artifact],
        model_content_sha256=digest,
        request_key="00000000-0000-4000-8000-000000000201",
    )
    assert operation.state == "succeeded", operation.last_error
    assert operation.artifact_set_sha256 is not None
    return selector, digest, operation.artifact_set_sha256


def test_finalization_gate_contention_defers_without_blocking_unrelated_removal(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        clock = [datetime.now(UTC)]
        service._clock = lambda: clock[0]
        model_a = _canonical_model(
            publisher="vonk-forge",
            slug="finalization-contention-a",
            file_id="weights",
            file_digest=hashlib.sha256(b"abc").hexdigest(),
        )
        selector_a = _register_model(sessions, model_a)
        digest_a, _artifact_a, set_a = _seed_model(
            service,
            tmp_path,
            model_a,
            "00000000-0000-4000-8000-000000000202",
        )
        object_a = hashlib.sha256(b"abc").hexdigest()
        object_path_a = service._object_path(object_a)
        selector_b, digest_b, _set_b = _seed_unrelated_model(
            service, sessions, tmp_path
        )
        request_key_a = "00000000-0000-4000-8000-000000000203"
        removal_a = service.remove_model_selector(
            selector_a,
            actor="operator",
            request_key=request_key_a,
            model_content_sha256=digest_a,
        )
        assert removal_a.state == "queued"
        assert service.advance_removals(limit=1) == 1
        assert not object_path_a.exists()
        assert service.advance_removals(limit=1) == 1
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is not None
        checkpoint = service.get_operation(removal_a.id).progress
        assert checkpoint.get("completed_artifacts") == 2
        assert checkpoint.get("total_artifacts") == 2

        # The unrelated owner is accepted only after A has passed its byte and
        # set checkpoints, so its later progress proves scheduler isolation.
        clock[0] += timedelta(seconds=1)
        request_key_b = "00000000-0000-4000-8000-000000000204"
        removal_b = service.remove_model_selector(
            selector_b,
            actor="operator",
            request_key=request_key_b,
            model_content_sha256=digest_b,
        )

        gate_holder = sessions()
        try:
            gate = gate_holder.scalar(
                select(ArtifactLifecycleGate)
                .where(
                    ArtifactLifecycleGate.artifact_kind == "model-set",
                    ArtifactLifecycleGate.artifact_sha256 == set_a,
                )
                .with_for_update(nowait=True)
            )
            assert gate is not None
            assert gate.removal_owner_id == removal_a.id

            # PostgreSQL NOWAIT contention at finalization is translated to a
            # retryable wait after the failed transaction unwinds. Earlier unlink
            # and membership checkpoints remain committed under A's same fence.
            assert service.advance_removals(limit=1) == 0
            deferred_a = service.get_operation(removal_a.id)
            assert deferred_a.state == "partial"
            assert deferred_a.retryable is True
            assert deferred_a.failure is not None
            assert deferred_a.failure.get("artifact_key") == "removal-finalization"
            delay = deferred_a.failure.get("retry_after_seconds")
            assert isinstance(delay, int) and 0 < delay <= 60
            assert deferred_a.request_key == request_key_a

            # A's retry deadline is in the future. B therefore finishes through
            # the real removal worker while A's unrelated gate row remains locked.
            settled_b: CacheOperationView = _settle_removal(service, removal_b)
            assert settled_b.state == "succeeded"
            assert service.get_operation(removal_a.id).state == "partial"
            assert gate.removal_owner_id == removal_a.id
        finally:
            gate_holder.rollback()
            gate_holder.close()

        clock[0] += timedelta(seconds=120)
        assert service.advance_removals(limit=1) == 1
        settled_a = service.get_operation(removal_a.id)
        assert settled_a.id == removal_a.id
        assert settled_a.request_key == request_key_a
        assert settled_a.state == "succeeded"
        assert isinstance(settled_a.result, ModelCacheRemovalResult)
        assert set_a in settled_a.result.removed_entries
        assert not object_path_a.exists()
    finally:
        service.close()
