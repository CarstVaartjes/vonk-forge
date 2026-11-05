"""A model of many files is admitted; a deterministic database refusal is named."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Text, cast, create_engine, func, select
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, RecipeImageCode
from vonk_control.admission_locking import ConstraintDefect, admitting
from vonk_control.lifecycle.model_cache import ModelCacheAdapter
from vonk_control.models import Base, ModelCacheOperation
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_model_cache import _artifact, cache  # noqa: F401
from .test_recipe_image_availability import (
    Transport,
    _add_revision,
    _download_preview,
    _manifest,
    _recipe,
    _runtime,
    _service,
)

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


def test_a_non_contention_database_error_during_admission_is_not_retried(
    cache,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _sessions = cache
    calls: list[object] = []

    def broken(**fields):
        calls.append(fields)
        raise DataError("INSERT", {}, Exception("value too long"))

    monkeypatch.setattr(ModelCacheAdapter, "new_operation", staticmethod(broken))
    with pytest.raises(DataError):
        _start(
            service,
            _files(tmp_path, 1),
            "00000000-0000-4000-8000-0000000000a3",
        )
    assert len(calls) == 1


def test_a_refused_model_queue_request_ends_the_preparation_with_its_detail(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-refused", recipe)
    starts: list[object] = []

    class ModelCache:
        def download_preview(self, **_: object) -> dict[str, object]:
            return _download_preview(
                plan_digest="d" * 64, artifact_set_sha256="c" * 64, new_bytes=100
            )

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                digest="c" * 64,
                document=lambda: _manifest(model_content_digests=["d" * 64]),
            )

        def reusable_operation(self, **_: object) -> None:
            return None

        def start_download(self, **_: object) -> object:
            # A real check violation, decided by the shared classification.
            starts.append(object())
            now = datetime.now(UTC)
            with admitting(), sessions.begin() as session:
                session.add(
                    ModelCacheAdapter.new_operation(
                        request_key=str(uuid.uuid4()),
                        schema_version=2,
                        kind="download",
                        attempt=0,
                        payload={},
                        progress={},
                        actor="test",
                        created_at=now,
                        updated_at=now,
                    )
                )
            raise AssertionError("the violation must have been raised")

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        model_cache=ModelCache(),
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start("revision-refused", actor="operator", request_id="r" * 36)
    assert service.run_pending() == 1
    ended = service.get(queued.id)
    assert ended.state == LifecycleState.FAILED, ended.failure_evidence
    assert ended.failure_evidence is not None
    assert ended.failure_evidence.code == RecipeImageCode.MODEL_CACHE_INVALID
    assert ended.failure_evidence.retryable is False
    assert "ck_model_cache_operations_attempt" in ended.failure_evidence.detail
    # It stopped resending: nothing more to run, one request was made.
    assert service.run_pending() == 0
    assert len(starts) == 1
