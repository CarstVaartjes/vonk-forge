"""A model of many files is admitted; a deterministic database refusal is named."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Text, cast, func, select
from sqlalchemy.exc import IntegrityError
from vonk_control.admission_locking import ConstraintDefect
from vonk_control.lifecycle.model_cache import ModelCacheAdapter
from vonk_control.model_cache.download import DownloadMixin
from vonk_control.models import ModelCacheOperation
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability.scheduling import _exhausted

from .test_model_cache import _artifact, cache  # noqa: F401

_OLD_PAYLOAD_CAP = 262144
_MODEL = "b" * 64


def _files(root: Path, count: int) -> list[dict[str, object]]:
    return [
        _artifact(
            root,
            f"file {index}".encode(),
            artifact_id=f"file-{index:04d}",
            path=f"shards/{'d' * 150}/model-{index:05d}-of-{count:05d}.safetensors",
            model_content_sha256=_MODEL,
        )
        for index in range(count)
    ]


def _start(service, artifacts, request_key: str):
    preview = service.download_preview(model_content_sha256=_MODEL, artifacts=artifacts)
    return service.start_download(
        actor="test",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=_MODEL,
        artifacts=artifacts,
    )


@pytest.mark.slow(30)  # 500 source files hashed and planned in one admission
def test_a_model_of_many_files_is_admitted_past_the_old_payload_cap(
    cache,  # noqa: F811
    tmp_path: Path,
) -> None:
    service, sessions = cache
    operation = _start(
        service,
        _files(tmp_path, 500),
        "00000000-0000-4000-8000-0000000000a1",
    )
    assert operation.state == "queued"
    with sessions() as session:
        size = session.scalar(
            select(func.length(cast(ModelCacheOperation.payload, Text))).where(
                ModelCacheOperation.id == operation.id
            )
        )
    assert size > _OLD_PAYLOAD_CAP


def test_a_check_violation_during_admission_is_a_typed_failure_at_once(
    cache,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _sessions = cache
    attempts: list[object] = []
    real = ModelCacheAdapter.new_operation

    def below_the_attempt_floor(**fields):
        attempts.append(fields)
        return real(**{**fields, "attempt": 0})

    monkeypatch.setattr(
        ModelCacheAdapter, "new_operation", staticmethod(below_the_attempt_floor)
    )
    with pytest.raises(ConstraintDefect) as defect:
        _start(
            service,
            _files(tmp_path, 1),
            "00000000-0000-4000-8000-0000000000a2",
        )
    assert defect.value.constraint == "ck_model_cache_operations_attempt"
    assert "observation budget" not in str(defect.value)
    # Deterministic: refused on the first attempt, never retried.
    assert len(attempts) == 1


def test_a_reference_observation_names_an_integrity_error() -> None:
    error = IntegrityError("INSERT", {}, Exception("CHECK constraint failed: x"))
    with pytest.raises(ConstraintDefect):
        DownloadMixin._reference_unknown(error)


def test_the_exhausted_failure_keeps_the_last_attempts_cause() -> None:
    last = AvailabilityOperationFailure(
        code="model_child_failed", detail="payload column refused the manifest"
    )
    kept = _exhausted(last)
    assert kept.code != last.code
    assert "model_child_failed" in kept.detail
    assert "payload column refused the manifest" in kept.detail
    assert _exhausted(kept) == kept
