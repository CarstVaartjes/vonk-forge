"""A removal yields per object to a newer set that lists it (newer request wins)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from vonk_control.models import ModelCacheSet, ModelCacheSetArtifact
from vonk_forge_contracts import document_sha256

from .removal_settle import advance_until_ended
from .test_model_cache import _artifact, _download, _remove_model
from .test_model_removal_reference_lifecycle import (
    _force_model_request,
    _one_model,
    _register_model,
    _removal_service,
)


def _download_set(service, tmp_path, name, blobs, model_digest, key):
    root = tmp_path / f"src-{name}"
    root.mkdir()
    artifacts = [
        _artifact(
            root,
            data,
            artifact_id=f"f{index}",
            path=f"f{index}.bin",
            model_content_sha256=model_digest,
        )
        for index, data in enumerate(blobs)
    ]
    for item in artifacts:
        item["roles"] = ["model"]
    op = _download(
        service, artifacts, model_content_sha256=model_digest, request_key=key
    )
    assert op.state == "succeeded", op.last_error
    return op.artifact_set_sha256


def _request_download(service, tmp_path, name, blobs, model_digest, key):
    return _download_set(service, tmp_path, name, blobs, model_digest, key)


def _key(tag: str, number: int) -> str:
    return f"00000000-0000-4000-8000-{tag}{number:09d}"


def _setup(postgres_engine, tmp_path, blobs, tag):
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model_a = _one_model(tmp_path, f"{tag}-a")
    model_b = _one_model(tmp_path, f"{tag}-b")
    selector_a = _register_model(sessions, model_a)
    selector_b = _register_model(sessions, model_b)
    digest_a = document_sha256(model_a.model_dump(mode="json"))
    digest_b = document_sha256(model_b.model_dump(mode="json"))
    set_a = _download_set(service, tmp_path, "a", blobs, digest_a, _key(tag, 1))
    removal = _remove_model(
        service,
        selector_a,
        actor="operator",
        request_key=_key(tag, 2),
        model_content_sha256=digest_a,
    )
    assert service._observe_model_removal_scope(removal.id)
    source_b = tmp_path / "source-b"
    source_b.mkdir()
    artifact_b = _artifact(source_b, b"abc", model_content_sha256=digest_b)
    download_b = _force_model_request(
        service,
        selector=selector_b,
        digest=digest_b,
        artifact=artifact_b,
        request_key=_key(tag, 3),
    )
    return service, sessions, selector_a, removal, download_b, set_a


def _path(service, data: bytes) -> Path:
    return service._object_path(hashlib.sha256(data).hexdigest())


def test_removal_keeps_object_a_newer_set_lists(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service, sessions, _sel, removal, download_b, set_a = _setup(
        postgres_engine, tmp_path, [b"abc"], "a01"
    )
    try:
        for _ in range(4):
            service._advance_model_removal(removal.id)
        # The shared object survives and the removal completes by yielding.
        assert _path(service, b"abc").exists()
        assert service.get_operation(removal.id).state == "succeeded"
        service.reconcile_requested_removals()
        service.reconcile_removal_gates()
        service.run_pending()
        service.run_pending()
        assert service.get_operation(download_b.id).state == "succeeded"
        assert _path(service, b"abc").exists()
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is None
    finally:
        service.close()


def test_half_done_removal_is_not_cancelled_and_leaves_no_broken_set(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service, sessions, _sel, removal, download_b, set_a = _setup(
        postgres_engine, tmp_path, [b"abc", b"exclusive-p"], "b01"
    )
    try:
        for _ in range(4):
            service._advance_model_removal(removal.id)
        service.reconcile_requested_removals()
        service.reconcile_removal_gates()
        advance_until_ended(service, removal.id)
        service.run_pending()
        service.run_pending()
        assert service.get_operation(removal.id).state == "succeeded"
        assert _path(service, b"abc").exists()
        assert not _path(service, b"exclusive-p").exists()
        with sessions() as session:
            row = session.get(ModelCacheSet, set_a)
            # No cached set may be left with a missing object.
            assert row is None or row.state != "cached"
        assert service.get_operation(download_b.id).state == "succeeded"
    finally:
        service.close()


def test_removal_does_not_park_behind_a_cancelled_newer_set(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service, sessions, _sel, removal, download_b, set_a = _setup(
        postgres_engine, tmp_path, [b"abc"], "c01"
    )
    try:
        service.cancel_operation(
            download_b.id,
            actor="operator",
            request_key=_key("c01", 4),
            reason="operator changed their mind",
        )
        for step in range(10):
            skew = timedelta(hours=step + 1)
            service._clock = lambda skew=skew: datetime.now(UTC) + skew
            service.tick()
            service.advance_removals(limit=1)
        assert service.get_operation(removal.id).state == "succeeded"
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is None
            assert not session.scalars(
                select(ModelCacheSetArtifact).where(
                    ModelCacheSetArtifact.artifact_set_sha256 == set_a
                )
            ).all()
        # A fresh request for the same bytes admits and succeeds afterwards.
        assert _request_download(
            service,
            tmp_path,
            "again",
            [b"abc"],
            download_b.model_content_sha256,
            _key("c01", 5),
        )
    finally:
        service.close()
