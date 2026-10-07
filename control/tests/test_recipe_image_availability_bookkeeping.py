"""Availability bookkeeping that cannot be read is retired or rebuilt, never refused."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, OperationProgress
from vonk_control.job_documents import AvailabilityJobPayload
from vonk_control.model_cache_contract import ModelCacheCancellation
from vonk_control.models import Base, Job, ModelCacheOperation, User
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    _removal_retry_is_due,
)
from vonk_control.recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.stored_json import Residue, read_row_column
from vonk_forge_contracts import RecipeDefinition

from .test_recipe_image_availability import (
    _add_revision,
    _recipe,
    _runtime,
    _service,
)


def _started(tmp_path: Path):
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'bookkeeping.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "bookkeeping-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    operation = service.start(
        "bookkeeping-revision", actor="operator", request_id="bookkeeping"
    )
    return engine, sessions, service, operation


def test_availability_concerns_cannot_be_recombined_into_an_oversized_module() -> None:
    """Catch moving split implementations back into one monolithic facade."""
    import vonk_control.recipe_image_availability as package

    assert package.__file__ is not None
    for source in Path(package.__file__).parent.glob("*.py"):
        assert len(source.read_text().splitlines()) < 1000, source.name


def test_a_malformed_stored_retry_time_makes_the_removal_due_instead_of_failing(
    caplog,
) -> None:
    # The stored model validates RFC 3339, so the reader is exercised with the
    # shape a hand-edited row could still produce past that check.
    damaged = cast(
        AvailabilityOperationFailure,
        SimpleNamespace(
            code="artifact.deletion_in_progress",
            retryable=True,
            retry_time="tomorrow-ish",
        ),
    )
    now = datetime.now(UTC)
    with caplog.at_level(logging.DEBUG):
        assert _removal_retry_is_due(damaged, now) is True
    assert any(
        getattr(record, "residue_kind", "") == "recipe-image.removal-retry-time"
        for record in caplog.records
    )
    # A well-formed future retry time still waits: the shortcut is only for damage.
    future = cast(
        AvailabilityOperationFailure,
        SimpleNamespace(
            code="artifact.deletion_in_progress",
            retryable=True,
            retry_time=(now + timedelta(minutes=5)).isoformat(),
        ),
    )
    assert _removal_retry_is_due(future, now) is False


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_cancellation_evidence_reads_as_none_and_a_fresh_cancel_heals_it(
    tmp_path: Path,
) -> None:
    engine, sessions, service, operation = _started(tmp_path)
    with sessions.begin() as session:
        job = session.get(Job, operation.id)
        assert job is not None
        job.payload = {**job.payload, "cancellation": {"cancel_requested": "maybe"}}
    with sessions() as session:
        job = session.get(Job, operation.id)
        assert job is not None
        assert service._stored_cancellation(job) is None
    cancelled = service.cancel(
        operation.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a01",
        reason="stop the preparation",
    )
    assert cancelled.state == "cancelled"
    assert cancelled.cancellation is not None
    # Repairing unrelated damage must retain an adopted cancellation, including
    # when the record also contains a retired field.
    with sessions.begin() as session:
        job = session.get(Job, operation.id)
        assert job is not None
        job.payload = dict(job.payload) | {
            "retry_after_at": "damaged",
            "cancellation": cancelled.cancellation.model_dump(mode="json")
            | {"retired_field": True},
        }
    observed = service.get(operation.id)
    assert observed.cancellation == cancelled.cancellation
    assert observed.state == "cancelled" and service.run_pending() == 0
    from .non_blocking import assert_ended_without_blocking

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        observed,
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "bookkeeping-revision", actor="operator", request_id="after-damaged-cancel"
        ),
    )
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_stored_request_reads_as_a_key_used_by_another_operation(
    tmp_path: Path,
) -> None:
    engine, sessions, service, operation = _started(tmp_path)
    with sessions.begin() as session:
        job = session.get(Job, operation.id)
        assert job is not None
        job.payload = {**job.payload, "request": {"kind": "not-an-intent"}}
    with pytest.raises(RecipeImageAvailabilityError) as refused:
        service.start(
            "bookkeeping-revision", actor="operator", request_id="bookkeeping"
        )
    assert refused.value.code == "recipe_image.request_key_reused"
    engine.dispose()


def test_a_damaged_stored_recipe_is_rebuilt_from_its_revision_under_its_digest(
    tmp_path: Path,
) -> None:
    engine, sessions, service, operation = _started(tmp_path)
    with sessions() as session:
        job = session.get(Job, operation.id)
        assert job is not None
        intact = dict(job.payload)
    readable = service._stored_recipe(intact)
    assert isinstance(readable, RecipeDefinition)
    assert readable.identity.slug == (_recipe("recipe-source-build.json").identity.slug)
    damaged = {**intact, "recipe": {"not": "a recipe"}}
    recovered = service._stored_recipe(damaged)
    assert isinstance(recovered, RecipeDefinition)
    assert recovered.identity == _recipe("recipe-source-build.json").identity
    # The rebuild is evidence only under the digest the operation was accepted
    # with: a revision with other content is never a source for it.
    other = {**damaged, "recipe_content_sha256": "e" * 64}
    assert isinstance(service._stored_recipe(other), Residue)
    engine.dispose()


def test_stored_damage_is_not_raised_as_an_invalid_operation_outside_the_removal_owner() -> (
    None
):
    """Only the removal-owner readers may still refuse on a damaged stored row.

    Every other reader of stored availability state heals, rebuilds or retires
    what it cannot read; a new ``recipe_image.operation_invalid`` raise elsewhere
    would bring the refusal back.
    """

    import ast

    import vonk_control.recipe_image_availability as module

    allowed = {"_read_removal_owner", "_read_removal_result"}
    assert module.__file__ is not None
    trees = [
        ast.parse(path.read_text())
        for path in Path(module.__file__).parent.glob("*.py")
    ]
    offenders: set[str] = set()
    for function in (node for tree in trees for node in ast.walk(tree)):
        if not isinstance(function, ast.FunctionDef) or function.name in allowed:
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and node.exc.args
                and (
                    isinstance(node.exc.args[0], ast.Constant)
                    and node.exc.args[0].value == "recipe_image.operation_invalid"
                    or isinstance(node.exc.args[0], ast.Attribute)
                    and ast.unparse(node.exc.args[0])
                    == "RecipeImageCode.OPERATION_INVALID"
                )
            ):
                offenders.add(function.name)
    assert not offenders, offenders


def test_builder_progress_survives_the_column_guard_and_completes(
    tmp_path: Path,
) -> None:
    """A progress callback must not poison the payload with unused detail keys."""
    engine, sessions, service, operation = _started(tmp_path)
    original_builder = service._builder
    assert original_builder is not None
    observed = []

    def build(*args, progress, **kwargs):
        progress(
            OperationProgress(
                phase="build", completed_bytes=7, total_bytes=10, total_bytes_known=True
            ).model_dump(mode="json")
        )
        with sessions() as session:
            row = session.get(Job, operation.id)
            assert row is not None
            readable = read_row_column(row, "payload")
            assert isinstance(readable, AvailabilityJobPayload)
            observed.append(readable.progress.completed_bytes)
        return original_builder(*args, **kwargs)

    service._builder = build
    assert service.run_pending() == 1
    assert observed == [7]
    assert service.get(operation.id).state == "succeeded"
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_optional_observations_and_legacy_fields_heal_in_the_worker(
    tmp_path: Path,
) -> None:
    """Readable intent survives damaged observations without a replacement request."""
    engine, sessions, service, operation = _started(tmp_path)
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "recipe": {"damaged": True},
            "retry_after_at": "tomorrow-ish",
            "model_child": {"id": "unreadable"},
            "image_result": {"image_bytes": "bad"},
            "step": "retired detail",
            "log_excerpt": "retired log",
        }
    # Reading does not rewrite the owner, and uses its exact catalog identity.
    assert service.get(operation.id).residue is None
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None and isinstance(read_row_column(row, "payload"), Residue)
    assert service.run_pending() == 1
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None and isinstance(
            read_row_column(row, "payload"), AvailabilityJobPayload
        )
    assert service.get(operation.id).state == "succeeded"
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_unreadable_identity_is_residue_and_does_not_block_other_claims(
    tmp_path: Path,
) -> None:
    """One unadoptable owner neither crashes reads nor consumes another job's slot."""
    engine, sessions, service, damaged = _started(tmp_path)
    ready = service.start("bookkeeping-revision", actor="operator", request_id="ready")
    with sessions.begin() as session:
        row = session.get(Job, damaged.id)
        assert row is not None
        row.payload = {
            "schema_version": 2,
            "recipe_revision_id": "bookkeeping-revision",
        }
    view = service.get(damaged.id)
    assert isinstance(view.residue, Residue)
    rows, total, _ = service.list_page(limit=10)
    assert total == 2 and any(row.residue is not None for row in rows)
    claims = service.claim_pending(limit=4)
    assert [claim.operation_id for claim in claims] == [ready.id]
    service.run_claim(claims[0])
    assert service.get(ready.id).state == "succeeded"
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_lost_terminal_evidence_is_residue_and_a_new_request_still_runs(
    tmp_path: Path,
) -> None:
    """A damaged result cannot masquerade as success or obstruct fresh intent."""
    engine, sessions, service, operation = _started(tmp_path)
    assert service.run_pending() == 1
    assert service.get(operation.id).state == "succeeded"
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        row.result = {"lost": True}
    unknown = service.get(operation.id)
    assert isinstance(unknown.residue, Residue)
    assert unknown.result is None and unknown.image_state == "succeeded"
    accepted = service.start(
        "bookkeeping-revision", actor="operator", request_id="after-lost-result"
    )
    assert service.run_pending() == 1
    assert service.get(accepted.id).state == "succeeded"
    engine.dispose()


def test_model_child_cancel_intent_survives_unrelated_payload_damage(
    tmp_path: Path,
) -> None:
    """A damaged download envelope must not forget its accepted cancellation."""
    from vonk_control.recipe_image_availability import RecipeImageAvailabilityService

    cancel = ModelCacheCancellation(
        request_key="00000000-0000-4000-8000-000000000a02",
        actor="operator",
        reason="stop the child",
        requested_at=datetime.now(UTC).isoformat(),
    )
    row = ModelCacheOperation(
        id="00000000-0000-4000-8000-000000000a03",
        kind="download",
        payload={
            "manifest": {"damaged": True},
            "cancellation": cancel.model_dump(mode="json"),
        },
    )
    assert isinstance(read_row_column(row, "payload"), Residue)
    assert RecipeImageAvailabilityService._model_child_has_cancel_intent(row)
    row.payload = {"cancellation": {"request_key": "damaged"}}
    assert not RecipeImageAvailabilityService._model_child_has_cancel_intent(row)
    from .non_blocking import assert_ended_without_blocking

    engine, sessions, service, parent = _started(tmp_path)

    def end_parent(receipt):
        cancelled = service.cancel(
            receipt.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000a04",
            reason="end the parent",
        )
        assert isinstance(cancelled, RecipeImageAvailabilityView)
        return cancelled

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        parent,
        end=end_parent,
        fresh=lambda _: service.start(
            "bookkeeping-revision", actor="operator", request_id="after-child-cancel"
        ),
    )
    engine.dispose()


def test_metadata_observation_retries_before_admission_and_then_accepts(
    tmp_path: Path,
) -> None:
    """A single metadata fault must not require another operator request."""
    engine, sessions, service, _ = _started(tmp_path)
    authority = service._authority
    attempts = 0

    def observe(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("metadata peer disconnected")
        return authority(*args, **kwargs)

    service._authority = observe
    accepted = service.start(
        "bookkeeping-revision", actor="operator", request_id="metadata-recovery"
    )
    assert accepted.state == "queued"
    assert attempts == 2
    with sessions() as session:
        assert session.get(Job, accepted.id) is not None
    engine.dispose()


def test_exhausted_metadata_observation_ends_without_holds_and_fresh_request_works(
    tmp_path: Path,
) -> None:
    """Permanent observation damage must not strand a request or synthesize a plan."""
    from .non_blocking import assert_ended_without_blocking

    engine, sessions, service, _ = _started(tmp_path)
    authority = service._authority

    def unavailable(*args, **kwargs):
        raise OSError("metadata peer unavailable")

    service._authority = unavailable
    observed = service.start(
        "bookkeeping-revision", actor="operator", request_id="metadata-unavailable"
    )
    assert observed.failure_evidence is not None
    assert observed.failure_evidence.code == "recipe_image.metadata_refresh_failed"
    assert observed.residue is not None
    assert observed.build_input_sha256 is None
    # This is a failed pre-admission observation, not a fabricated accepted Job.
    with sessions() as session:
        assert session.get(Job, observed.id) is None
    service._authority = authority
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        observed,
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "bookkeeping-revision", actor="operator", request_id="metadata-restored"
        ),
        assert_reason=_assert_typed_failure,
    )
    engine.dispose()


def test_a_gone_model_child_ends_parent_and_admits_fresh_preparation(
    tmp_path: Path,
) -> None:
    """A vanished exact child must not keep its parent's execution claim retrying."""
    from vonk_control.job_documents import AvailabilityModelChild
    from vonk_control.model_cache import ModelCacheNotFound
    from vonk_control.strict_json import serialize_json_value

    from .non_blocking import assert_ended_without_blocking

    engine, sessions, service, operation = _started(tmp_path)
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = read_row_column(row, "payload")
        assert isinstance(payload, AvailabilityJobPayload)
        row.payload = serialize_json_value(
            payload.model_copy(
                update={
                    "model_child": AvailabilityModelChild(
                        id="00000000-0000-4000-8000-000000000201",
                        state=LifecycleState.QUEUED,
                    )
                }
            )
        )

    def missing(operation_id: str):
        raise ModelCacheNotFound("model_cache.operation_missing", operation_id)

    service._model_cache = SimpleNamespace(get_operation=missing)

    def end(receipt):
        assert service.run_pending() == 1
        return service.get(receipt.id)

    ended, _ = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _: service.start(
            "bookkeeping-revision", actor="operator", request_id="after-child-loss"
        ),
        assert_reason=_assert_typed_failure,
    )
    assert ended.failure_evidence is not None
    assert ended.failure_evidence.code == "recipe_image.model_child_missing"
    with sessions() as session:
        row = session.get(Job, ended.id)
        assert row is not None
        payload = read_row_column(row, "payload")
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.claim_owner is None and payload.claim_until is None
    engine.dispose()


def test_terminal_removal_releases_storage_gate_before_a_fresh_preparation(
    tmp_path: Path,
) -> None:
    """A terminal child failure must release the image gate in the same worker pass."""
    import json

    from vonk_control.models import ArtifactLifecycleGate

    from .non_blocking import assert_ended_without_blocking
    from .recipe_removal_review_support import remove_after_review
    from .runtime_image_fixtures import place_test_image
    from .test_recipe_image_availability import (
        ARCHIVE,
        ARCHIVE_SHA,
        _add_head,
        _reference_receipt,
    )

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'terminal-removal.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "terminal-removal", recipe))
        session.add(User(subject="operator", role="operator"))
    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(
        json.dumps(_reference_receipt().model_dump(mode="json"))
    )
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *args, **kwargs: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    accepted = remove_after_review(
        service,
        "vonk-forge/synthetic-tiny-build",
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a02",
    )
    operation_id = str(accepted["operation_id"])
    with sessions() as session:
        gate = session.get(ArtifactLifecycleGate, ("runtime-image", ARCHIVE_SHA))
        assert gate is not None and gate.removal_owner_id == operation_id
    operation = service.get_operator_operation(operation_id)

    def end(receipt):
        assert service._record_recipe_removal_failure(
            operation_id,
            code="model_cache.removal_child_missing",
            detail="the exact child is gone",
            retryable=False,
        )
        return service.get_operator_operation(operation_id)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _: service.start(
            "terminal-removal", actor="operator", request_id="after-terminal-removal"
        ),
        assert_reason=_assert_typed_failure,
    )
    engine.dispose()


def _assert_typed_failure(receipt: object) -> None:
    if isinstance(receipt, RecipeImageAvailabilityView):
        failure = receipt.failure_evidence
    else:
        assert isinstance(receipt, RecipeCacheRemovalStatus)
        failure = receipt.failure
    assert failure is not None and failure.code
    assert failure.recovery_actions == []


def test_metadata_security_refusal_is_preserved_without_an_admission_retry(
    tmp_path: Path,
) -> None:
    """A denied authority must never become a retryable metadata observation."""
    from vonk_agent_protocol import SecurityRefusalError, SecurityRefusalReason

    engine, _sessions, service, _ = _started(tmp_path)
    denied = SecurityRefusalError(
        "catalog authentication required",
        reason=SecurityRefusalReason.CATALOG_AUTHENTICATION_REQUIRED,
    )
    calls = 0

    def refuse(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise denied

    service._authority = refuse
    with pytest.raises(SecurityRefusalError) as observed:
        service.start(
            "bookkeeping-revision", actor="operator", request_id="denied-metadata"
        )
    assert observed.value is denied and calls == 1
    engine.dispose()


@pytest.mark.parametrize(
    "fault", ["cache-bookkeeping", "database", "malformed-document"]
)
def test_child_observation_fault_does_not_refuse_read_or_discard_verified_image(
    tmp_path: Path,
    fault: str,
) -> None:
    """A read fault must not turn verified image availability into a refusal."""
    from sqlalchemy.exc import SQLAlchemyError
    from vonk_control.job_documents import AvailabilityModelChild
    from vonk_control.model_cache import ModelCacheError
    from vonk_control.strict_json import serialize_json_value

    engine, sessions, service, operation = _started(tmp_path)
    assert service.run_pending() == 1
    verified = service.get(operation.id).artifact
    assert verified is not None
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = read_row_column(row, "payload")
        assert isinstance(payload, AvailabilityJobPayload)
        row.payload = serialize_json_value(
            payload.model_copy(
                update={
                    "model_child": AvailabilityModelChild(
                        id="00000000-0000-4000-8000-000000000201",
                        state=LifecycleState.SUCCEEDED,
                    ),
                }
            )
        )
    failure = (
        ModelCacheError(
            "model_cache.operation_invalid", "cache bookkeeping unavailable"
        )
        if fault == "cache-bookkeeping"
        else SQLAlchemyError("read interrupted")
        if fault == "database"
        else ValueError("stored child document malformed")
    )

    def observe(operation_id: str):
        raise failure

    service._model_cache = SimpleNamespace(get_operation=observe)
    observed = service.get(operation.id)
    assert observed.state == "succeeded"
    assert observed.artifact == verified
    assert observed.residue is not None
    page, _, _ = service.list_page()
    assert page[0].artifact == verified and page[0].residue is not None
    engine.dispose()
