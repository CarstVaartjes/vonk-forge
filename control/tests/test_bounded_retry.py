"""A damaged read is a value, and a request-led refusal is retried a bounded number
of times before the requester hears of it."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from vonk_control import recipe_operations as recipe_operations_module
from vonk_control.admission_locking import AdmissionLockBusy, admission_wait_exhausted
from vonk_control.bounded_retry import REQUEST_PAUSES, bounded_attempts
from vonk_control.install_admission import InstallAdmissionBusy
from vonk_control.lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
)
from vonk_control.recipe_operations import (
    RecipeBuildOwnershipBusy,
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionBusy


def test_bounded_attempts_pause_between_repeats_only() -> None:
    pauses: list[float] = []

    attempts = list(bounded_attempts((1.0, 2.0), sleep=pauses.append))

    assert attempts == [0, 1, 2]
    assert len(pauses) == 2
    assert 0.5 <= pauses[0] <= 1.5 and 1.0 <= pauses[1] <= 3.0


def test_the_request_schedule_is_short() -> None:
    assert sum(REQUEST_PAUSES) < 1.0


def test_a_returned_damaged_document_is_rebuilt_from_evidence() -> None:
    value = read_or_rebuild(
        kind="test",
        subject="one",
        read=lambda: Damaged("stored value is invalid"),
        rebuild=lambda: 7,
    )

    assert value == 7


def test_a_returned_damaged_document_without_evidence_is_retired_as_unknown() -> None:
    value = read_or_rebuild(
        kind="test",
        subject="two",
        read=lambda: Damaged("stored value is invalid"),
        rebuild=lambda: None,
    )

    assert isinstance(value, Residue)
    assert value.reason is BookkeepingReason.PERSISTED_STATE_DAMAGED
    assert "stored value is invalid" in value.note


def test_a_readable_document_is_returned_as_is() -> None:
    assert read_or_rebuild(kind="test", subject="three", read=lambda: 3) == 3


class _Flaky:
    """An operation whose first ``refusals`` attempts meet a busy admission."""

    def __init__(self, refusals: int, error: Exception) -> None:
        self.refusals = refusals
        self.error = error
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> str:
        self.calls += 1
        if self.calls <= self.refusals:
            raise self.error
        return "accepted"


@pytest.fixture
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recipe_operations_module,
        "admission_attempts",
        lambda: iter(range(3)),
    )


def _service() -> RecipeOperationService:
    # The wrappers read no state of their own: only the attempt methods do.
    return object.__new__(RecipeOperationService)


def test_install_repeats_a_busy_admission_until_it_is_accepted(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    flaky = _Flaky(2, InstallAdmissionBusy("install.capacity_busy"))
    monkeypatch.setattr(RecipeOperationService, "_install_once", flaky)

    result = _service().install(
        object(),  # type: ignore[arg-type]
        plan_digest="d",
        actor="a",
        request_id="r",
    )

    assert result == "accepted"
    assert flaky.calls == 3


def test_install_reports_the_last_busy_admission_after_the_attempts(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    flaky = _Flaky(99, InstallAdmissionBusy("install.capacity_busy"))
    monkeypatch.setattr(RecipeOperationService, "_install_once", flaky)

    with pytest.raises(InstallAdmissionBusy):
        _service().install(
            object(),  # type: ignore[arg-type]
            plan_digest="d",
            actor="a",
            request_id="r",
        )

    assert flaky.calls == 3


def test_start_repeats_a_busy_run_capacity_writer(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    flaky = _Flaky(1, RunAdmissionBusy("run capacity writer is busy"))
    monkeypatch.setattr(RecipeOperationService, "_start_once", flaky)

    result = _service().start(
        object(),  # type: ignore[arg-type]
        plan_digest="d",
        actor="a",
        request_id="r",
    )

    assert result == "accepted"
    assert flaky.calls == 2


def test_start_returns_after_the_implicit_sql_wait_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    class LockTimeout(Exception):
        sqlstate = "55P03"

    busy = RunAdmissionBusy("run capacity writer is busy")
    busy.__cause__ = OperationalError("INSERT INTO jobs", {}, LockTimeout())
    flaky = _Flaky(99, busy)
    monkeypatch.setattr(RecipeOperationService, "_start_once", flaky)

    with pytest.raises(RunAdmissionBusy) as returned:
        _service().start(
            object(),  # type: ignore[arg-type]
            plan_digest="d",
            actor="a",
            request_id="same-request",
        )

    assert returned.value is busy
    assert flaky.calls == 1


def test_immediate_nowait_refusal_keeps_its_request_retry(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    busy = RunAdmissionBusy("run capacity writer is busy")
    busy.__cause__ = AdmissionLockBusy("row is busy", sqlstate="55P03")
    assert not admission_wait_exhausted(busy)
    flaky = _Flaky(2, busy)
    monkeypatch.setattr(RecipeOperationService, "_start_once", flaky)

    assert (
        _service().start(
            object(),  # type: ignore[arg-type]
            plan_digest="d",
            actor="a",
            request_id="same-request",
        )
        == "accepted"
    )
    assert flaky.calls == 3


def test_a_build_cancellation_repeats_a_lock_another_writer_holds(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    flaky = _Flaky(
        2, RecipeBuildOwnershipBusy("build.consumer_busy: ownership is changing")
    )
    monkeypatch.setattr(RecipeOperationService, "_cancel_current_build", flaky)

    result = _service()._cancel_build("job", actor="a", request_id="r", reason="stop")

    assert result == "accepted"
    assert flaky.calls == 3


def test_other_refusals_are_not_repeated(
    monkeypatch: pytest.MonkeyPatch, no_pauses: None
) -> None:
    flaky = _Flaky(99, ValueError("not a busy admission"))
    monkeypatch.setattr(RecipeOperationService, "_install_once", flaky)

    with pytest.raises(ValueError):
        _service().install(
            object(),  # type: ignore[arg-type]
            plan_digest="d",
            actor="a",
            request_id="r",
        )

    assert flaky.calls == 1
