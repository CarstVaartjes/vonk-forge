"""A removal yields per object to a newer set that lists it (newer request wins)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from vonk_control.models import (
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
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


def _artifacts(tmp_path, name, blobs, model_digest):
    root = tmp_path / f"src-{name}"
    root.mkdir(exist_ok=True)
    items = [
        _artifact(
            root,
            data,
            artifact_id=f"f{index}",
            path=f"f{index}.bin",
            model_content_sha256=model_digest,
        )
        for index, data in enumerate(blobs)
    ]
    for item in items:
        item["roles"] = ["model"]
    return items


def _request_set(service, selector, tmp_path, name, blobs, model_digest, key):
    artifacts = _artifacts(tmp_path, name, blobs, model_digest)
    preview = service.download_preview(
        model_content_sha256=model_digest, artifacts=artifacts
    )
    return service.start_download(
        actor="operator",
        request_key=key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model_digest,
        artifacts=artifacts,
        force=True,
        selector=selector,
    )


def test_touched_removal_yields_its_set_to_a_newer_download_of_it(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "same-a")
    selector = _register_model(sessions, model)
    digest = document_sha256(model.model_dump(mode="json"))
    blobs = [b"abc", b"exclusive-p"]
    set_a = _download_set(service, tmp_path, "a", blobs, digest, _key("d01", 1))
    try:
        removal = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=_key("d01", 2),
            model_content_sha256=digest,
        )
        assert service._observe_model_removal_scope(removal.id)
        service._advance_model_removal(removal.id)  # deletes every object
        assert not _path(service, b"abc").exists()
        newer = _request_set(
            service, selector, tmp_path, "a", blobs, digest, _key("d01", 3)
        )
        advance_until_ended(service, removal.id)
        assert service.get_operation(removal.id).state == "succeeded"
        with sessions() as session:
            row = session.get(ModelCacheSet, set_a)
            assert row is not None and row.state != "cached"
        service.run_pending()
        service.run_pending()
        assert service.get_operation(newer.id).state == "succeeded"
        assert all(_path(service, data).exists() for data in blobs)
        with sessions() as session:
            final = session.get(ModelCacheSet, set_a)
            assert final is not None and final.state == "cached"
    finally:
        service.close()


def test_yielded_set_whose_request_is_cancelled_is_still_reclaimable(
    postgres_engine: Engine, tmp_path: Path, monkeypatch
) -> None:
    from vonk_control.model_cache import removal_execution

    monkeypatch.setattr(
        removal_execution, "REMOVAL_BATCH_SECONDS", 0
    )  # one object per step
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "two-sets")
    selector = _register_model(sessions, model)
    digest = document_sha256(model.model_dump(mode="json"))
    shared, p_only, q_only = b"abc", b"only-p", b"only-q"
    set_one = [shared, p_only]
    _download_set(service, tmp_path, "s1", set_one, digest, _key("d02", 1))
    _download_set(service, tmp_path, "s2", [shared, q_only], digest, _key("d02", 2))
    everything = (shared, p_only, q_only)
    try:
        removal = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=_key("d02", 3),
            model_content_sha256=digest,
        )
        assert service._observe_model_removal_scope(removal.id)
        service._advance_model_removal(removal.id)  # one object; now touched
        newer = _request_set(
            service, selector, tmp_path, "s1", set_one, digest, _key("d02", 4)
        )
        advance_until_ended(service, removal.id)
        assert service.get_operation(removal.id).state == "succeeded"
        service.cancel_operation(
            newer.id,
            actor="operator",
            request_key=_key("d02", 5),
            reason="operator changed their mind",
        )
        with sessions() as session:
            listed = set(session.scalars(select(ModelCacheSetArtifact.artifact_sha256)))
        for data in everything:
            if _path(service, data).exists():
                assert hashlib.sha256(data).hexdigest() in listed  # not stranded
        # A fresh removal reclaims whatever is left.
        again = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=_key("d02", 6),
            model_content_sha256=digest,
        )
        advance_until_ended(service, again.id)
        assert service.get_operation(again.id).state == "succeeded"
        assert not any(_path(service, data).exists() for data in everything)
        with sessions() as session:
            assert not session.scalars(select(ModelCacheSet)).all()
        # And the same bytes can be requested again.
        fresh = _request_set(
            service, selector, tmp_path, "s1", set_one, digest, _key("d02", 7)
        )
        service.run_pending()
        assert service.get_operation(fresh.id).state == "succeeded"
    finally:
        service.close()


def test_object_kept_for_an_owner_that_is_then_cancelled_is_not_stranded(
    postgres_engine: Engine, tmp_path: Path, monkeypatch
) -> None:
    from vonk_control.model_cache import removal_execution

    monkeypatch.setattr(removal_execution, "REMOVAL_BATCH_SECONDS", 0)
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "stranded")
    selector = _register_model(sessions, model)
    digest = document_sha256(model.model_dump(mode="json"))
    shared, p_only, q_only = b"abc", b"only-p", b"only-q"
    set_one = [shared, p_only]
    _download_set(service, tmp_path, "s1", set_one, digest, _key("d03", 1))
    _download_set(service, tmp_path, "s2", [shared, q_only], digest, _key("d03", 2))
    everything = (shared, p_only, q_only)
    try:
        removal = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=_key("d03", 3),
            model_content_sha256=digest,
        )
        assert service._observe_model_removal_scope(removal.id)
        service._advance_model_removal(removal.id)  # one object; now touched
        newer = _request_set(
            service, selector, tmp_path, "s1", set_one, digest, _key("d03", 4)
        )
        cancelled = False
        for _ in range(40):
            current = service.get_operation(removal.id)
            if current.state == "succeeded":
                break
            with sessions() as session:
                row = session.get(ModelCacheOperation, removal.id)
                assert row is not None
                payload = dict(row.payload)
            targets = payload["delete_objects"]
            assert isinstance(targets, list)
            if not cancelled and payload["object_index"] == len(targets):
                # Every object was visited while the owner held its set.
                service.cancel_operation(
                    newer.id,
                    actor="operator",
                    request_key=_key("d03", 5),
                    reason="operator changed their mind",
                )
                cancelled = True
                break
            advance_until_ended(service, removal.id, rounds=1)
        assert cancelled
        advance_until_ended(service, removal.id)
        assert service.get_operation(removal.id).state == "succeeded"
        with sessions() as session:
            listed = set(session.scalars(select(ModelCacheSetArtifact.artifact_sha256)))
        for data in everything:
            if _path(service, data).exists():
                assert hashlib.sha256(data).hexdigest() in listed  # not stranded
        # The released set is reclaimable by the next removal as well.
        fresh = _request_set(
            service, selector, tmp_path, "s1", set_one, digest, _key("d03", 6)
        )
        service.run_pending()
        assert service.get_operation(fresh.id).state == "succeeded"
    finally:
        service.close()
