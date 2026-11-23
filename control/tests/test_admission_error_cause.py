"""A deterministic admission error ends the request; only an I/O fault waits.

Production shape: run and install admission read stored documents inside the
acceptance transaction.  A damaged one raised a ``ValueError`` (a pydantic
``ValidationError``) that was wrapped as ``RecipeRetryLater(str(error))``, so the
owner retried a request that fails identically on every attempt.
"""

import errno

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation,
    Disposition,
    InvalidRequestError,
    UnknownOutcomeError,
)
from vonk_control import run_admission
from vonk_control.failure_classification import disposition
from vonk_control.models import Job, RecipeInstallation, RecipeRun
from vonk_forge_contracts import read_recipe

from .test_recipe_operations import installed_recipe, setup_services


def _damaged_stored_recipe(_document):
    return read_recipe({})


def _start(tmp_path, monkeypatch, read):
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 36
    )
    plan = service.preview_run(installed.owner_id, "qwen")
    monkeypatch.setattr(run_admission, "read_recipe", read)
    try:
        service.start(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
        )
    except Exception as error:  # noqa: BLE001 - the test inspects the outcome
        return sessions, error
    raise AssertionError("start was accepted")


def _nothing_started(sessions):
    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        assert (
            session.scalar(
                select(Job).where(Job.kind == AgentOperation.RECIPE_START.value)
            )
            is None
        )


def test_start_with_a_damaged_stored_recipe_ends_with_the_cause(tmp_path, monkeypatch):
    sessions, error = _start(tmp_path, monkeypatch, _damaged_stored_recipe)

    assert isinstance(error, InvalidRequestError)
    assert not isinstance(error, UnknownOutcomeError)
    assert disposition(error) is Disposition.FAIL
    assert "run admission" in str(error)
    assert "ValidationError" in str(error)
    _nothing_started(sessions)


@pytest.mark.parametrize(
    "code,waits", [(errno.EIO, True), (errno.ENOENT, False)], ids=["eio", "enoent"]
)
def test_start_storage_fault_waits_only_when_it_can_clear(
    tmp_path, monkeypatch, code, waits
):
    def unreadable(_document):
        raise OSError(code, "stored recipe document is unreadable")

    sessions, error = _start(tmp_path, monkeypatch, unreadable)

    assert isinstance(error, UnknownOutcomeError) is waits
    assert isinstance(error, InvalidRequestError) is not waits
    assert disposition(error) is (Disposition.WAIT if waits else Disposition.FAIL)
    # Either way the step and the cause are named.
    assert "run admission" in str(error)
    assert "stored recipe document is unreadable" in str(error)
    _nothing_started(sessions)


@pytest.mark.parametrize(
    "fault,waits",
    [
        (OSError(errno.EIO, "managed storage read failed"), True),
        (TypeError("compiled plan field is not a mapping"), False),
    ],
    ids=["eio", "type-error"],
)
def test_install_receipt_refresh_waits_only_on_a_fault_that_can_clear(
    tmp_path, monkeypatch, fault, waits
):
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)

    def refresh(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr(service._install_admission, "refresh_install_receipts", refresh)

    with pytest.raises((UnknownOutcomeError, InvalidRequestError)) as raised:
        service.install(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
        )

    assert isinstance(raised.value, UnknownOutcomeError) is waits
    assert disposition(raised.value) is (
        Disposition.WAIT if waits else Disposition.FAIL
    )
    assert "install receipt refresh" in str(raised.value)
    assert str(fault) in str(raised.value)
    with sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
