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
from vonk_control.models import Base, Job, User
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    _removal_retry_is_due,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

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
    engine.dispose()


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
    assert service._stored_recipe(intact).identity.slug == (
        _recipe("recipe-source-build.json").identity.slug
    )
    damaged = {**intact, "recipe": {"not": "a recipe"}}
    assert (
        service._stored_recipe(damaged).identity
        == _recipe("recipe-source-build.json").identity
    )
    # The rebuild is evidence only under the digest the operation was accepted
    # with: a revision with other content is never a source for it.
    other = {**damaged, "recipe_content_sha256": "e" * 64}
    with pytest.raises(RecipeImageAvailabilityError) as refused:
        service._stored_recipe(other)
    assert refused.value.code == "recipe_image.recipe_invalid"
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
    tree = ast.parse(Path(module.__file__).read_text())
    offenders: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or function.name in allowed:
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and node.exc.args
                and isinstance(node.exc.args[0], ast.Constant)
                and node.exc.args[0].value == "recipe_image.operation_invalid"
            ):
                offenders.add(function.name)
    assert not offenders, offenders
