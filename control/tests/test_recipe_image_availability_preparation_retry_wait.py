"""Recipe image availability: preparation retry wait."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.jobs.documents import (
    AvailabilityJobPayload,
    AvailabilityModelChild,
)
from vonk_control.lifecycle.image_availability import PREPARATION_STALL_WINDOW
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import (
    User,
)
from vonk_control.models.jobs import Job
from vonk_control.operations.blockers import make_blocker
from vonk_control.operations.contract import (
    AvailabilityOperationFailure,
)
from vonk_control.recipe_image_availability.contracts import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityUnknown,
)
from vonk_control.recipe_image_availability.service import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations.lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from vonk_control.runtime_image_preparation.contracts import (
    RuntimeImagePreparationError,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.strict_json import serialize_json_value

from .recipe_image_availability_support import (
    ARCHIVE_SHA,
    Transport,
    _add_head,
    _add_revision,
    _availability_payload,
    _build_runtime,
    _builder,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
    _set_active_head,
    _successor,
)
from .test_model_removal_worker_postgres import _prepare_removal_target


def test_image_preparation_retries_with_capped_backoff_until_it_succeeds(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-flaky", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    failures = 4

    class FlakyTransport(Transport):
        def inspect_archive(self, archive, **kwargs):
            if self.calls < failures * 3:
                self.calls += 1
                raise RuntimeError("registry says permission denied, digest unknown")
            return super().inspect_archive(archive, **kwargs)

    transport = FlakyTransport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    accepted = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="2" * 36
    )
    delays = []
    for _ in range(failures):
        assert service.run_pending() == 1
        waiting = service.get(accepted.id)
        assert waiting.state == "queued"
        assert waiting.failure is not None
        assert waiting.failure["retryable"] is True
        delays.append(waiting.failure["retry_after_seconds"])
        next_attempt = service.get(accepted.id).next_attempt_at
        assert next_attempt is not None
        now[0] = datetime.fromisoformat(next_attempt)
    # The core's one bounded, jittered clock: it grows, never past its cap.
    assert min(delays) >= 1
    assert max(delays) <= 90
    assert delays[-1] > delays[0]
    assert max(delays) <= 900
    assert service.run_pending() == 1
    assert service.get(accepted.id).state == "succeeded"


def test_archive_integrity_failure_builds_the_image_again(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-integrity", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    transport = Transport()
    storage = FilesystemRuntimeImageStorage(tmp_path)
    original = storage.existing_archive
    corrupt = [True]

    def verify_once(archive_sha256: str, expected_bytes: int) -> Path:
        if corrupt[0]:
            corrupt[0] = False
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch", "stored archive failed verification"
            )
        return original(archive_sha256, expected_bytes)

    builds: list[bool] = []
    service = _service(
        sessions,
        storage=storage,
        builder=_builder(storage, calls=builds),
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    first = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="3" * 36
    )
    service.run_pending()
    assert service.get(first.id).state == "succeeded"
    storage.existing_archive = verify_once  # type: ignore[method-assign]
    again = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="4" * 36
    )
    for _ in range(5):
        if service.get(again.id).state == "succeeded":
            break
        service.run_pending()
        waiting = service.get(again.id)
        if waiting.state != LifecycleState.SUCCEEDED:
            assert waiting.next_attempt_at is not None
            now[0] = datetime.fromisoformat(waiting.next_attempt_at)
    assert service.get(again.id).state == "succeeded"
    # The mismatch was observed and the image was built again, not reused.
    assert corrupt == [False]
    assert builds[-1] is True
    assert storage.read_receipt(ARCHIVE_SHA).oci_archive_sha256 == ARCHIVE_SHA


def test_a_succeeded_preparation_the_plan_still_misses_is_asked_again(
    tmp_path: Path,
) -> None:
    """A load asks only for what its plan lacks, so an earlier success is stale.

    Replaying the first success (the image was removed afterwards) would park
    the load for ever; the next ask starts a fresh preparation, paced.
    """

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-evicted", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )

    def preparations() -> list[Job]:
        with sessions() as session:
            return list(
                session.scalars(
                    select(Job).where(Job.kind == "recipe.image.availability.v2")
                )
            )

    service.ensure_preparation("revision-evicted", actor="operator")
    for _ in range(5):
        if service.run_pending() == 0:
            break
        now[0] += timedelta(seconds=1)
    assert [job.state for job in preparations()] == ["succeeded"]

    # Just finished: say so, do not spin another one.
    blockers = service.ensure_preparation("revision-evicted", actor="operator")
    assert [item.severity for item in blockers] == ["info"]
    assert len(preparations()) == 1

    # Still missing after the pause: a new preparation is requested.
    now[0] += timedelta(minutes=3)
    blockers = service.ensure_preparation("revision-evicted", actor="operator")
    assert not any(item.severity == "error" for item in blockers)
    assert len(preparations()) == 2


def test_newer_revision_is_prepared_at_once_after_the_older_build_failed(
    tmp_path: Path,
) -> None:
    """The quiet period after a failed build only guards the SAME revision."""

    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        older = _add_revision(session, "revision-older", recipe, built=False)
        newer = _add_revision(session, "revision-newer", successor, built=False)
        newer.document_id = older.document_id
        newer.revision_number = 2
        _add_head(session, older)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_failed",
            "compiler failed at step 4",
            retryable=False,
            recovery_actions=("inspect",),
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-older" else successor,
            _build_runtime(),
        ),
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    service.ensure_preparation("revision-older", actor="operator")
    assert service.run_pending() == 1
    blockers = service.ensure_preparation("revision-older", actor="operator")
    assert blockers
    with sessions() as session:
        prior = session.scalar(
            select(Job).where(Job.authority_revision == "revision-older")
        )
        assert prior is not None
        assert prior.next_action_at is not None
        assert prior.payload.get("claim_owner") is None

    # The recipe syncs a newer revision; no waiting for the retry pause.
    with sessions.begin() as session:
        _set_active_head(session, "revision-newer")
    blockers = service.ensure_preparation("revision-newer", actor="operator")

    assert not any(item.severity == "error" for item in blockers)
    with sessions() as session:
        started = tuple(
            session.scalars(
                select(Job).where(Job.kind == "recipe.image.availability.v2")
            )
        )
    assert any(
        job.payload.get("recipe_revision_id") == "revision-newer"
        and job.state in {"queued", "running"}
        for job in started
    ), [(job.kind, job.state, job.payload) for job in started]


def _parked_operation(
    operation_id: str, *, now: datetime, model_child: bool, updated: datetime
) -> Job:
    payload = _availability_payload(f"revision-{operation_id}")
    if model_child:
        payload = payload.model_copy(
            update={
                "model_child": AvailabilityModelChild(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id)),
                    state=LifecycleState.RUNNING,
                ),
                "image_result": _reference_receipt(),
            }
        )
    return Job(
        id=operation_id,
        request_id=operation_id.ljust(36, "x"),
        kind="recipe.image.availability.v2",
        state=LifecycleState.BACKOFF.value if model_child else "queued",
        actor="operator",
        authority_revision=f"revision-{operation_id}",
        targets=[f"revision-{operation_id}"],
        payload_digest="a" * 64,
        payload=serialize_json_value(payload),
        result=None,
        current_attempt=1 if model_child else 0,
        created_at=now,
        updated_at=updated,
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_operations_waiting_only_on_a_model_download_take_no_worker_slot(
    tmp_path: Path,
) -> None:
    """Ready work is claimed first; model waiters are parked with a named reason."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime(2026, 10, 2, 1, 10, tzinfo=UTC)
    with sessions.begin() as session:
        # Many waiters are older than the ready operation, as in production.
        for index in range(40):
            session.add(
                _parked_operation(
                    f"waiter{index:03d}",
                    now=now,
                    model_child=True,
                    updated=now - timedelta(minutes=30 - index // 2),
                )
            )
        session.add(_parked_operation("ready", now=now, model_child=False, updated=now))

    class ModelCache:
        def get_operation(self, operation_id: str) -> SimpleNamespace:
            return SimpleNamespace(state="running", blockers=())

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (None, {}),
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    claims = service.claim_pending(limit=4)
    assert [claim.operation_id for claim in claims] == ["ready"]
    with sessions() as session:
        waiter = session.get(Job, "waiter000")
        assert waiter is not None
        assert waiter.state == LifecycleState.BACKOFF
        assert waiter.current_attempt == 1
        blockers = require_sequence(waiter.payload["blockers"], "blockers")
        assert [require_mapping(item, "blocker")["code"] for item in blockers] == [
            "recipe_image.waiting_for_model"
        ]
    # Parked waiters are not re-examined until their poll is due.
    assert service.claim_pending(limit=4) == ()


def test_parked_operation_carries_the_model_childs_typed_wait(tmp_path: Path) -> None:
    """A gated model's access wait reaches the parent operation."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime(2026, 10, 2, 1, 10, tzinfo=UTC)
    url = "https://huggingface.co/org/gated"
    with sessions.begin() as session:
        waiter = _parked_operation("gated", now=now, model_child=True, updated=now)
        session.add(waiter)

    class ModelCache:
        def get_operation(self, operation_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                state="backoff",
                blockers=(
                    make_blocker(
                        "access_denied",
                        f"Hugging Face access is required: accept the terms at {url}",
                    ),
                ),
            )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (None, {}),
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    service.claim_pending(limit=4)
    with sessions() as session:
        parked = session.get(Job, "gated")
        assert parked is not None
        blockers = require_sequence(parked.payload["blockers"], "blockers")
        assert [require_mapping(item, "blocker")["code"] for item in blockers] == [
            "access_denied"
        ]
        assert url in str(require_mapping(blockers[0], "blocker")["detail"])


def test_live_download_survives_a_parent_observation_gap_and_resumes(
    tmp_path: Path,
    postgres_engine,
) -> None:
    """A stale parent checkpoint cannot cancel a live child after worker restart."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    now = [datetime.now(UTC)]
    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, parent_id))
    old = now[0] - PREPARATION_STALL_WINDOW * 2
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
        session.add(
            _parked_operation(parent_id, now=old, model_child=True, updated=old)
        )

    @dataclass
    class ModelOwner:
        state: LifecycleState = LifecycleState.RUNNING
        blockers: tuple[()] = ()

        def get_operation(self, operation_id: str) -> ModelOwner:
            assert operation_id == child_id
            return self

    model_owner = ModelOwner()

    def reopen() -> RecipeImageAvailabilityService:
        return _service(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *_args, **_kwargs: (None, {}),
            model_cache=model_owner,
            clock=lambda: now[0],
        )

    service = reopen()
    assert service.claim_pending() == ()
    with sessions() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        assert parent.state == LifecycleState.BACKOFF
        payload = service._payload(parent)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.cancellation is None
    # Another scheduler gap must reconnect to the same persisted parent/child,
    # and completion of the child makes the parent claimable again.
    now[0] += PREPARATION_STALL_WINDOW * 2
    service = reopen()
    assert service.claim_pending() == ()
    model_owner.state = LifecycleState.SUCCEEDED
    now[0] += timedelta(minutes=1)
    (claim,) = service.claim_pending()
    assert claim.operation_id == parent_id
    with sessions() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        assert parent.state == LifecycleState.RUNNING
        payload = service._payload(parent)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.cancellation is None


@pytest.mark.parametrize("checkpointed", [False, True])
def test_preparation_expiry_preserves_download_but_explicit_cancel_still_owns_it(
    tmp_path: Path, postgres_engine, checkpointed: bool
) -> None:

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    cache = ModelCacheService(
        sessions, tmp_path / "models", free_fraction=0, fixture_sources=True
    )
    try:
        selector, _, _ = _prepare_removal_target(
            cache, sessions, tmp_path, slug="observer-budget", data=b"abc"
        )
        revision_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())
        child_request = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:recipe-availability-model:{revision_id}:{request_id}",
            )
        )
        child = cache.download_model_selector(
            selector, actor="operator", request_key=child_request
        )
        assert child.state == LifecycleState.QUEUED
        payload = _availability_payload(revision_id).model_copy(
            update={
                "model_child": (
                    AvailabilityModelChild(
                        id=child.id,
                        state=LifecycleState.QUEUED,
                        request_key=child.request_key,
                    )
                    if checkpointed
                    else None
                ),
                "failure": AvailabilityOperationFailure(
                    code=RecipeImageCode.PREPARATION_EXHAUSTED,
                    detail="Image preparation observation budget ended",
                    retryable=False,
                    recovery_actions=[],
                ),
            }
        )
        cancellation = RecipeOperationCancellationResult(
            cancel_requested=True,
            cancel_request_id=str(uuid.uuid4()),
            cancel_actor="operator",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
        service = _service(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *_args, **_kwargs: (None, {}),
            model_cache=cache,
            clock=lambda: datetime.now(UTC),
        )
        assert service._model_child_cancellation_pending(
            parent_id, payload, request_id, cancellation
        ) == (False, False)
        retained = cache.get_operation(child.id)
        assert retained.state == LifecycleState.QUEUED
        assert retained.cancellation is None
        assert retained.request_key == child_request
        # Explicit cancellation still reaches this exact owned download, even
        # if a crash prevented checkpointing its ID on the parent.
        service._model_child_cancellation_pending(
            parent_id,
            payload.model_copy(update={"failure": None}),
            request_id,
            cancellation.model_copy(update={"cancel_request_id": str(uuid.uuid4())}),
        )
        stopped = cache.get_operation(child.id)
        assert stopped.state == LifecycleState.CANCELLED
        assert stopped.cancellation is not None
    finally:
        cache.close()


def test_unknown_outcome_is_raised_not_failed_and_same_key_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch an unaccepted request becoming a synthetic terminal FAILED view."""
    monkeypatch.setattr(
        "vonk_control.recipe_image_availability.requests."
        "REQUEST_OBSERVATION_BUDGET_SECONDS",
        0,
    )
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(session, "unknown-outcome", recipe)
        _add_head(session, revision)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=None,
        clock=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )
    request_id = "00000000-0000-4000-8000-000000000077"
    plan = SimpleNamespace(
        recipe_build_id="build", recipe_revision_id="unknown-outcome"
    )

    with pytest.raises(RecipeImageAvailabilityUnknown):
        service.start("unknown-outcome", actor="operator", request_id=request_id)
    with pytest.raises(RecipeImageAvailabilityUnknown):
        service.prepare_install(plan, "operator", "install-1")  # type: ignore[arg-type]

    service._authority = lambda *_a, **_k: (recipe, _runtime())
    view = service.start("unknown-outcome", actor="operator", request_id=request_id)
    assert view.state != LifecycleState.FAILED.value
    assert view.request_id == request_id
