"""Unrelated unavailable metadata must not obstruct request-led preparation."""

from pathlib import Path

import pytest
from vonk_agent_protocol import SecurityRefusalReason
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationRefused,
)

from .test_runtime_image_preparation import (
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    _prepare,
)


def test_unreadable_unrelated_receipt_is_a_miss_then_fresh_preparation_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)
    stale = storage.root / f"{'0' * 64}.receipt.json"
    stale.write_text("{}")
    original = Path.read_text

    def unavailable(path: Path, *args, **kwargs):
        if path == stale:
            raise OSError("temporarily unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unavailable)
    assert (
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    for archive_sha256 in (None, ARCHIVE_DIGEST):
        assert (
            storage.find_build(
                "a" * 64,
                expected_architecture="linux/arm64",
                expected_runtime_interface="vonk.runtime.v1",
                expected_archive_sha256=archive_sha256,
            )
            is None
        )
    receipt = _prepare(storage=storage)
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt


def test_preparation_does_not_swallow_denied_receipt_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)

    def denied(_digest: str):
        raise RuntimeImagePreparationRefused(
            SecurityRefusalReason.PERMISSION_DENIED,
            "receipt access denied",
            reason=SecurityRefusalReason.PERMISSION_DENIED,
        )

    monkeypatch.setattr(storage, "read_receipt", denied)
    with pytest.raises(RuntimeImagePreparationRefused):
        _prepare(storage=storage)


def test_missing_exact_build_receipt_is_a_lookup_miss(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)
    assert (
        storage.find_build(
            "a" * 64,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
            expected_archive_sha256=ARCHIVE_DIGEST,
        )
        is None
    )


def test_unknown_receipt_attempt_ends_without_blocking_fresh_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual persisted availability claims end and a new request can prepare."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import LifecycleState
    from vonk_control.models import Base, Job, RecipeBuild
    from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt

    from .test_recipe_image_availability import (
        ARCHIVE_SHA,
        IMAGE_DIGEST,
        Transport,
        _add_revision,
        _build_id,
        _builder,
        _recipe,
        _runtime,
        _service,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'operations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_revision(session, "receipt-recovery", recipe)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    transport = Transport()
    build_calls: list[bool] = []
    now = [datetime.now(UTC)]
    transfer = _builder(storage, calls=build_calls)

    def observe_build(*args, **kwargs):
        with sessions() as session:
            build = session.get(RecipeBuild, _build_id("receipt-recovery"))
            assert build is not None
            assert build.image_digest is not None
            assert build.oci_layout_sha256 is not None
            assert build.image_bytes is not None
            if not storage.build_archive_available(
                build.oci_layout_sha256, build.image_bytes
            ):
                transfer(*args, **kwargs)
            return AvailabilityBuildReceipt(
                state=LifecycleState.SUCCEEDED,
                build_id=build.id,
                build_input_sha256=build.build_input_sha256,
                image_digest=build.image_digest,
                oci_layout_sha256=build.oci_layout_sha256,
                image_bytes=build.image_bytes,
            ).model_dump(mode="json")

    def service():
        return _service(
            sessions,
            storage=storage,
            transport=transport,
            builder=observe_build,
            authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
            clock=lambda: now[0],
        )

    current = service()
    initial = current.start("receipt-recovery", actor="operator", request_id="initial")
    current.run_pending()
    assert current.get(initial.id).state == LifecycleState.SUCCEEDED
    path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    original = Path.read_text

    def unreadable(candidate: Path, *args, **kwargs):
        if candidate == path:
            raise PermissionError("local receipt unavailable")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    pending = current.start("receipt-recovery", actor="operator", request_id="unknown")
    stale_claim = current.claim_pending(owner_id="interrupted-receipt-worker")[0]
    current.run_claim(stale_claim)
    concurrent = current.start(
        "receipt-recovery", actor="operator", request_id="while-unknown"
    )
    assert concurrent.state == LifecycleState.QUEUED
    assert concurrent.id != pending.id
    for _ in range(12):
        current.run_pending()
        if current.get(pending.id).state == LifecycleState.FAILED:
            break
        now[0] += timedelta(hours=1)
        current = service()  # reload durable owner/retry state
    with sessions() as session:
        ended = session.get(Job, pending.id)
        assert ended is not None
        assert ended.payload.get("claim_owner") is None
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        current.get(pending.id),
        end=lambda receipt: receipt,
        fresh=lambda _: current.start(
            "receipt-recovery", actor="operator", request_id="fresh"
        ),
    )
    monkeypatch.setattr(Path, "read_text", original)
    for _ in range(3):
        current.run_pending()
        if current.get(fresh.id).state == LifecycleState.SUCCEEDED:
            break
    completed = current.get(fresh.id)
    assert completed.state == LifecycleState.SUCCEEDED, completed.failure
    assert storage.read_receipt(ARCHIVE_SHA).image_digest == IMAGE_DIGEST
    inspections = transport.calls
    current.run_claim(stale_claim)
    assert transport.calls == inspections
    assert len(build_calls) == 1
    with storage.publication_lock(ARCHIVE_SHA):
        pass
    engine.dispose()
