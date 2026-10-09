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


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "fault", ["dependency", "unknown", "io", "payload", "future_retry"]
)
def test_observation_budget_is_owned_by_request_across_restart_for_all_unknowns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import (
        LifecycleState,
        RecipeImageCode,
        RuntimeImageCode,
        WaitReason,
    )
    from vonk_control.models import Base, Job
    from vonk_control.recipe_image_availability import BuildUnsettled
    from vonk_control.runtime_image_preparation import RuntimeImagePreparationUnknown

    from .test_recipe_image_availability import (
        _add_revision,
        _builder,
        _recipe,
        _runtime,
        _service,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'budget.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_revision(session, "budget", recipe)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    now = [datetime.now(UTC)]
    blocked = [True]
    verified_builder = _builder(storage)

    def builder(*args, **kwargs):
        if not blocked[0]:
            return verified_builder(*args, **kwargs)
        if fault == "dependency":
            return BuildUnsettled(
                RecipeImageCode.BUILD_WAIT,
                "dependency unavailable",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if fault == "unknown":
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.RECEIPT_UNAVAILABLE,
                "evidence unavailable",
                retryable=True,
                reason=WaitReason.RECEIPT_MISSING,
            )
        raise OSError("temporary local I/O failure")

    def owner():
        return _service(
            sessions,
            storage=storage,
            builder=builder,
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            clock=lambda: now[0],
        )

    service = owner()
    pending = service.start("budget", actor="operator", request_id="pending")
    if fault == "payload":
        with sessions.begin() as session:
            row = session.get(Job, pending.id)
            assert row is not None
            row.payload = {}
            row.payload["unreadable"] = True
    service.run_pending()
    assert service.get(pending.id).result is None
    if fault == "future_retry":
        import json

        from vonk_control.job_documents import AvailabilityJobPayload
        from vonk_control.strict_json import serialize_json_value

        with sessions.begin() as session:
            row = session.get(Job, pending.id)
            assert row is not None
            payload = AvailabilityJobPayload.model_validate_json(
                json.dumps(row.payload)
            )
            row.payload = serialize_json_value(
                payload.model_copy(
                    update={"retry_after_at": now[0] + timedelta(days=1)}
                )
            )
    now[0] += timedelta(minutes=16)
    service = owner()
    service.run_pending()
    from vonk_control.lifecycle.image_availability import CANCEL_BUDGET

    now[0] += CANCEL_BUDGET
    service.run_pending()
    assert service.get(pending.id).state == LifecycleState.CANCELLED
    with sessions() as session:
        row = session.get(Job, pending.id)
        assert row is not None
        assert row.payload.get("claim_owner") is None
        assert row.payload.get("claim_until") is None
    fresh = service.start("budget", actor="operator", request_id="fresh")
    assert fresh.id != pending.id
    blocked[0] = False
    service.run_pending()
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED
    engine.dispose()


def _hold_availability_row(database_url: str, operation_id: str, pipe) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from vonk_control.models import Job

    engine = create_engine(database_url)
    try:
        with sessionmaker(engine).begin() as session:
            session.scalar(select(Job).where(Job.id == operation_id).with_for_update())
            pipe.send(True)
            if not pipe.poll(20):
                raise TimeoutError("concurrent owner did not release test lock")
            pipe.recv()
    finally:
        engine.dispose()
        pipe.close()


@pytest.mark.postgres
@pytest.mark.linux_only
@pytest.mark.slow(60)
def test_postgres_concurrent_owner_newer_intent_fences_stale_claim_and_restarts(
    tmp_path: Path,
    postgres_engine,
) -> None:
    import json
    import multiprocessing
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import LifecycleState, RecipeImageCode, WaitReason
    from vonk_control.job_documents import AvailabilityJobResult
    from vonk_control.models import Base, Job
    from vonk_control.recipe_image_availability import BuildUnsettled

    from .test_recipe_image_availability import (
        _add_recipe_successors,
        _builder,
        _recipe,
        _runtime,
        _service,
        _set_active_head,
        _successor,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    newer = _successor(recipe, "new accepted intent")
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="old-owner",
            older=recipe,
            newer_id="new-owner",
            newer=newer,
        )
    now = [datetime.now(UTC)]
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    calls = []
    verified = _builder(storage, calls=calls)
    blocked = [True]

    def build(*args, **kwargs):
        if blocked[0]:
            return BuildUnsettled(
                RecipeImageCode.BUILD_WAIT,
                "dependency observation unavailable",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return verified(*args, **kwargs)

    def owner():
        return _service(
            sessions,
            storage=storage,
            builder=build,
            authority=lambda revision_id, **_: (
                newer if revision_id == "new-owner" else recipe,
                _runtime(),
            ),
            clock=lambda: now[0],
        )

    service = owner()
    old = service.start("old-owner", actor="operator", request_id="old")
    stale = service.claim_pending(owner_id="old-process")[0]
    service.run_claim(stale)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    postgres_engine.dispose()
    process = context.Process(
        target=_hold_availability_row,
        args=(postgres_engine.url.render_as_string(hide_password=False), old.id, child),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() is True
        with sessions.begin() as session:
            _set_active_head(session, "new-owner")
        accepted = service.start("new-owner", actor="operator", request_id="new")
        blocked[0] = False
        # The unrelated row is locked by a real process. SKIP LOCKED must let
        # this newer authorized owner prepare without waiting for that process.
        service.run_pending()
        assert service.get(accepted.id).state == LifecycleState.SUCCEEDED
        parent.send(True)
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent.close()
    now[0] += timedelta(minutes=16)
    service = owner()
    service.run_pending()
    from vonk_control.lifecycle.image_availability import CANCEL_BUDGET

    now[0] += CANCEL_BUDGET
    service.run_pending()
    assert service.get(old.id).state in (
        LifecycleState.CANCELLED,
        LifecycleState.FAILED,
    )
    before = len(calls)
    service.run_claim(stale)
    assert len(calls) == before
    assert service.get(accepted.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        row = session.get(Job, old.id)
        assert row is not None
        assert row.payload.get("claim_owner") is None
    accepted_document = service.get(accepted.id).result
    assert accepted_document is not None
    accepted_result = AvailabilityJobResult.model_validate_json(
        json.dumps(accepted_document)
    )
    receipt_path = storage.root / f"{accepted_result.oci_archive_sha256}.receipt.json"
    published_receipt = receipt_path.read_bytes()
    fresh = service.start(
        "new-owner", actor="operator", request_id="fresh-after-ending"
    )
    assert fresh.id != old.id
    service.run_pending()
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED
    fresh_document = service.get(fresh.id).result
    assert fresh_document is not None
    fresh_result = AvailabilityJobResult.model_validate_json(json.dumps(fresh_document))
    assert fresh_result.image_digest == accepted_result.image_digest
    assert fresh_result.oci_archive_sha256 == accepted_result.oci_archive_sha256
    assert receipt_path.read_bytes() == published_receipt
