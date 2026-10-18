"""One failure disposition: a wait never spends a retry, a deterministic
failure ends on its first occurrence with its cause, and only an uncertain
effect spends the retry count.

Each class is proven by what the work does (the lifecycle row it ends in, the
schedule it keeps), not by which helper decided it.
"""

from __future__ import annotations

import errno
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from vonk_agent_protocol import (
    FAILURE_KIND_DISPOSITION,
    HTTP_STATUS_DISPOSITIONS,
    WAIT_REASON_DISPOSITION,
    AgentFailureKind,
    Disposition,
    InvalidRequestError,
    InvalidRequestReason,
    ModelCacheCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    http_status_disposition,
)
from vonk_control.admission_locking import AdmissionLockBusy
from vonk_control.failure_classification import disposition, is_definite
from vonk_control.lifecycle import (
    Claimed,
    Effect,
    Execute,
    Lifecycle,
    Outcome,
    Reported,
    State,
    Tick,
    transition,
)
from vonk_control.model_cache.errors import (
    ModelCacheConflictInvalid,
    ModelCacheNotFoundInvalid,
    ModelCacheResolutionInvalid,
    ModelCacheStorageInvalid,
)
from vonk_control.recovery_policy import RECOVERY, RecoveryPolicy, schedule

from cluster_profiles import cli_states_generated as cli_words
from cluster_profiles.control_client.common import observation_disposition
from cluster_profiles.control_client.errors import ControlHTTPError

from .test_lifecycle_core import FakeAdapter

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def test_every_failure_kind_and_wait_reason_has_one_disposition() -> None:
    # A new contract word without a row would be classified by accident.
    assert set(FAILURE_KIND_DISPOSITION) == set(AgentFailureKind)
    assert set(WAIT_REASON_DISPOSITION) == set(WaitReason)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (400, Disposition.FAIL),
        (401, Disposition.SECURITY),
        (403, Disposition.SECURITY),
        (404, Disposition.FAIL),
        (408, Disposition.FAIL),
        (409, Disposition.FAIL),
        (412, Disposition.FAIL),
        (410, Disposition.FAIL),
        (422, Disposition.FAIL),
        (425, Disposition.FAIL),
        (429, Disposition.WAIT),
        (500, Disposition.WAIT),
        (503, Disposition.WAIT),
        (200, Disposition.OBSERVE),
    ],
)
def test_one_http_status_table_for_controller_and_cli(
    status: int, verdict: Disposition
) -> None:
    assert http_status_disposition(status) is verdict
    # The CLI ships without the contract package; its generated copy of the
    # table must give the same verdict.
    assert cli_words.HTTP_STATUS_DISPOSITIONS == tuple(
        (first, last, value.value) for first, last, value in HTTP_STATUS_DISPOSITIONS
    )
    if status >= 400:
        error = ControlHTTPError(status, "refused")
        assert observation_disposition(error) == verdict.value


class _Declared(RuntimeError):
    disposition = Disposition.WAIT


@pytest.mark.parametrize(
    ("error", "verdict"),
    [
        (ValueError("bad input"), Disposition.FAIL),
        (
            InvalidRequestError("x", reason=InvalidRequestReason.MALFORMED),
            Disposition.FAIL,
        ),
        (
            SecurityRefusalError("denied", reason=SecurityRefusalReason.HTTP_403),
            Disposition.SECURITY,
        ),
        # An unknown is observed (bounded); a named wait or a busy owner is a wait.
        (UnknownOutcomeError("evidence unavailable"), Disposition.OBSERVE),
        (
            UnknownOutcomeError("unread", reason=WaitReason.OBSERVATION_UNAVAILABLE),
            Disposition.OBSERVE,
        ),
        (
            UnknownOutcomeError("upgrade", reason=WaitReason.UPGRADE_AWAITING_IDENTITY),
            Disposition.WAIT,
        ),
        (AdmissionLockBusy("held"), Disposition.WAIT),
        (
            UnknownOutcomeError("stop?", reason=WaitReason.STOP_UNCONFIRMED),
            Disposition.OBSERVE,
        ),
        (OSError(errno.ENOSPC, "No space left on device"), Disposition.WAIT),
        (OSError(errno.EACCES, "Permission denied"), Disposition.FAIL),
        (OSError("no condition named"), Disposition.OBSERVE),
        (ConnectionResetError(), Disposition.WAIT),
        (httpx2.ConnectError("refused"), Disposition.WAIT),
        (_Declared("declared"), Disposition.WAIT),
    ],
)
def test_disposition_of_an_error(error: BaseException, verdict: Disposition) -> None:
    assert disposition(error) is verdict
    assert is_definite(error) is (verdict in {Disposition.FAIL, Disposition.SECURITY})


def test_a_wrapper_keeps_the_verdict_of_its_cause() -> None:
    for cause, verdict in (
        (OSError(errno.ENOSPC, "full"), Disposition.WAIT),
        (
            SecurityRefusalError("denied", reason=SecurityRefusalReason.HTTP_401),
            Disposition.SECURITY,
        ),
    ):
        wrapped = RuntimeError("phase failed")
        wrapped.__cause__ = cause
        assert disposition(wrapped) is verdict


@pytest.mark.parametrize(
    ("cause", "retryable", "consumes"),
    [
        (Disposition.WAIT, True, False),
        (Disposition.OBSERVE, True, True),
        (Disposition.FAIL, False, True),
        (Disposition.SECURITY, False, True),
        (AgentFailureKind.TEMPORARY_DEPENDENCY, True, False),
        (AgentFailureKind.RESOURCE_PREREQUISITE, True, False),
        (AgentFailureKind.UNCERTAIN_EFFECT, True, True),
        (AgentFailureKind.INVALID_CONTRACT, False, True),
    ],
)
def test_a_failed_report_derives_its_retry_from_its_cause(
    cause: Disposition | AgentFailureKind, retryable: bool, consumes: bool
) -> None:
    event = Reported.failed(cause, fence="f1")
    assert event.outcome is Outcome.FAILED
    assert event.retryable is retryable
    assert event.consumes_retry is consumes


def test_a_deterministic_error_report_keeps_its_cause_as_evidence() -> None:
    event = Reported.failed(ValueError("plan field `model` is not a digest"))
    assert not event.retryable
    assert event.reason == "ValueError: plan field `model` is not a digest"


def _run(
    adapter: FakeAdapter, row: Lifecycle, now: datetime
) -> tuple[Lifecycle, datetime]:
    """Claim the row's next attempt once it is due."""

    if row.next_action_at is not None:
        now = max(now, row.next_action_at)
    row = transition(row, Tick(), adapter, now).row
    claimed = transition(
        row,
        Claimed(row.attempt + 1, f"f{row.attempt + 1}", now + timedelta(minutes=1)),
        adapter,
        now,
    )
    return claimed.row, now


@pytest.mark.parametrize("irreversible", [False, True])
@pytest.mark.parametrize(
    "kind",
    # A dependency that is not there yet, and a Spark whose disk is full.
    [AgentFailureKind.TEMPORARY_DEPENDENCY, AgentFailureKind.RESOURCE_PREREQUISITE],
)
def test_a_dependency_wait_longer_than_the_retry_limit_still_succeeds(
    irreversible: bool, kind: AgentFailureKind
) -> None:
    # Wrong implementation: each dependency wait spent a retry, so the sixth
    # wait for a busy owner ended the order failed although nothing was wrong.
    # Irreversible work waits only from a report that positively stopped
    # before execution (the agent's pre-execution report says so).
    adapter = FakeAdapter(is_irreversible=irreversible)
    row = Lifecycle(id="order", kind="fake", state=State.QUEUED, effect=Effect.NONE)
    now = NOW
    for _wait in range(RECOVERY.max_failures * 3):
        row, now = _run(adapter, row, now)
        assert row.state is State.RUNNING
        decision = transition(
            row,
            Reported.failed(
                kind,
                fence=row.fence,
                effect=Effect.NONE if irreversible else None,
            ),
            adapter,
            now,
        )
        row = decision.row
        assert row.state is State.BACKOFF
        assert not row.terminal, "a wait ended the work"
        assert row.retry_count == 0, "a wait spent a retry"
    # The dependency clears: the next attempt runs and succeeds.
    row, now = _run(adapter, row, now)
    done = transition(row, Reported(Outcome.DONE, fence=row.fence), adapter, now)
    assert done.row.state is State.SUCCEEDED


def test_a_deterministic_failure_ends_on_its_first_occurrence_with_its_cause() -> None:
    adapter = FakeAdapter()
    row, now = _run(
        adapter, Lifecycle(id="order", kind="fake", effect=Effect.NONE), NOW
    )
    cause = ValueError("recipe argument `--tp` must be a power of two")
    ended = transition(row, Reported.failed(cause, fence=row.fence), adapter, now).row
    assert ended.state is State.FAILED
    assert ended.retry_count == 0
    assert ended.reason is not None and "power of two" in ended.reason
    # The end is the request's, not the queue's: a fresh request runs at once.
    fresh = Lifecycle(id="fresh", kind="fake", effect=Effect.NONE)
    assert transition(fresh, Tick(), adapter, now).commands == (Execute(),)


def test_only_a_recurring_uncertain_effect_spends_the_retry_count() -> None:
    adapter = FakeAdapter()
    row = replace(
        Lifecycle(id="order", kind="fake", effect=Effect.NONE), state=State.QUEUED
    )
    now = NOW
    for spent in range(1, RECOVERY.max_failures + 1):
        row, now = _run(adapter, row, now)
        row = transition(
            row,
            Reported.failed(AgentFailureKind.UNCERTAIN_EFFECT, fence=row.fence),
            adapter,
            now,
        ).row
        if row.terminal:
            break
        assert row.retry_count == spent
    assert row.state is State.FAILED
    fresh = Lifecycle(id="fresh", kind="fake", effect=Effect.NONE)
    assert transition(fresh, Tick(), adapter, now).commands == (Execute(),)


def test_an_owner_outside_the_core_schedules_on_the_same_rule() -> None:
    policy = RecoveryPolicy(max_failures=3, base_delay_seconds=5, max_delay_seconds=60)
    failures = 0
    for _ in range(policy.max_failures * 4):
        step = schedule(policy, "route", failures, Disposition.WAIT, NOW)
        assert step.failures == failures == 0
        assert step.next_at is not None and step.next_at > NOW
    assert schedule(policy, "route", 0, Disposition.FAIL, NOW).next_at is None
    assert schedule(policy, "route", 0, Disposition.SECURITY, NOW).next_at is None
    for spent in range(1, policy.max_failures + 1):
        step = schedule(policy, "route", failures, Disposition.OBSERVE, NOW)
        failures = step.failures
        assert failures == spent
    assert step.next_at is None


# ------------------------------------------------------------------ the guard

_GUARD_PATH = "control/src/vonk_control/example.py"


@pytest.mark.parametrize(
    "source",
    [
        "event = Reported(Outcome.FAILED, reason='x')\n",
        "event = Reported(Outcome.UNKNOWN, retryable=True)\n",
        "event = Reported(outcome, reason='x')\n",
        "event = replace(event, consumes_retry=False)\n",
        "POLICY = RecoveryPolicy(max_failures=6)\n",
        "ended = count + 1 >= RECOVERY.max_failures\n",
        "run.route_attempts += 1\n",
        "attempts = int(run.route_attempts or 0) + 1\n",
        "if run.recovery_attempts >= _RECOVERY_MAX_ATTEMPTS:\n    pass\n",
        (
            "def defer(run, error):\n"
            "    busy = isinstance(error, RecipeRouteOwnerBusy)\n"
            "    schedule(run, ongoing_intent=busy)\n"
        ),
    ],
)
def test_the_guard_finds_a_hand_classified_retry(source: str) -> None:
    from .deterministic_retries import scan_dispositions

    assert scan_dispositions(source, path=_GUARD_PATH)


@pytest.mark.parametrize(
    "source",
    [
        "event = Reported.failed(error, fence=fence)\n",
        "event = Reported(Outcome.DONE, fence=fence)\n",
        "event = Reported(Outcome.UNKNOWN, reason='unconfirmed')\n",
        # A local, deadline-bounded loop counter decides no retry.
        "attempts = 0\nwhile monotonic() < deadline:\n    attempts += 1\n",
        # A wait type that routes to its own handler, with no count in sight.
        (
            "def finish(error):\n"
            "    if isinstance(error, _ArtifactWriterBusy):\n"
            "        return defer(error)\n"
        ),
    ],
)
def test_the_guard_leaves_the_one_disposition_alone(source: str) -> None:
    from .deterministic_retries import scan_dispositions

    assert not scan_dispositions(source, path=_GUARD_PATH)


def test_the_owners_of_the_primitive_may_build_it() -> None:
    from .deterministic_retries import DISPOSITION_OWNERS, scan_dispositions

    for owner in DISPOSITION_OWNERS:
        assert not scan_dispositions(
            "POLICY = RecoveryPolicy(max_failures=6)\n", path=owner
        )


def test_no_failure_is_classified_outside_the_one_disposition() -> None:
    # Whole tree: every Controller and CLI site reports, counts and limits a
    # failure through ``disposition``/``Reported.failed``/``recovery_policy``.
    from pathlib import Path

    from .deterministic_retries import scan_dispositions

    root = Path(__file__).resolve().parents[2]
    found = [
        f"{path.relative_to(root)}:{line}: {kind}"
        for directory in (root / "control/src", root / "src")
        for path in sorted(directory.rglob("*.py"))
        for line, kind in scan_dispositions(
            path.read_text(), path=str(path.relative_to(root))
        )
    ]
    assert found == []


def test_a_distribution_identity_mismatch_is_definite_and_unavailability_observed() -> (
    None
):
    # Seen live: "assignment model objects do not match a verified cache
    # manifest" was a transient unknown, so the same mismatch was re-planned
    # every minute forever.  A mismatch is definite (#1476 names it); a
    # manifest that cannot be read stays an observed unknown.
    from vonk_agent_protocol import DistributionCode
    from vonk_control.distribution.types import DistributionError, DistributionUnknown

    mismatch = DistributionError(
        DistributionCode.MODEL_SET_MISMATCH, "assignment model objects differ"
    )
    assert disposition(mismatch) is Disposition.FAIL
    assert is_definite(mismatch)
    unavailable = DistributionUnknown(
        DistributionCode.OBJECT_UNAVAILABLE, "stored object is unavailable"
    )
    assert disposition(unavailable) is Disposition.OBSERVE
    assert not is_definite(unavailable)


def test_the_agent_reads_the_exported_status_table() -> None:
    # The Rust agent holds its status match to this export
    # (rust/crates/vonk-agent/tests/failure_disposition.rs).
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    exported = json.loads(
        (
            root / "agent_protocol/tests/fixtures/http_status_dispositions.json"
        ).read_text()
    )
    assert exported == {
        "statuses": {
            str(status): http_status_disposition(status).value
            for status in range(100, 600)
        }
    }


@pytest.mark.parametrize(
    "line",
    [
        "    if matches!(controller.status, 401 | 403) {\n",
        "    if error.status() == Some(403) {\n",
        "    } else if status == 409 && code == superseded {\n",
    ],
)
def test_the_agent_guard_finds_a_hand_read_status(line: str) -> None:
    from .deterministic_retries import scan_rust_dispositions

    path = "rust/crates/vonk-agent/src/executor/heartbeat.rs"
    assert scan_rust_dispositions(line, path=path)
    # The classifier itself owns the table.
    assert not scan_rust_dispositions(
        line, path="rust/crates/vonk-agent/src/client/errors.rs"
    )


def test_no_agent_lane_reads_an_http_status_by_hand() -> None:
    from pathlib import Path

    from .added_line_guards import rust_test_lines
    from .deterministic_retries import scan_rust_dispositions

    root = Path(__file__).resolve().parents[2]
    found = [
        f"{path.relative_to(root)}:{line}: {kind}"
        for path in sorted((root / "rust/crates/vonk-agent/src").rglob("*.rs"))
        if "/tests/" not in str(path) and not path.name.endswith("tests.rs")
        for line, kind in scan_rust_dispositions(
            path.read_text(),
            path=str(path.relative_to(root)),
            test_lines=rust_test_lines(path.read_text()),
        )
    ]
    assert found == []


@pytest.mark.parametrize(
    "error_type,code",
    [
        (ModelCacheConflictInvalid, ModelCacheCode.STALE_PLAN),
        (ModelCacheResolutionInvalid, ModelCacheCode.MODEL_PIN_INVALID),
        (ModelCacheStorageInvalid, ModelCacheCode.PAYLOAD_INVALID),
        (ModelCacheNotFoundInvalid, ModelCacheCode.SELECTOR_MISSING),
    ],
)
def test_invalid_cache_errors_end_instead_of_spending_observation_attempts(
    error_type, code
):
    """The cache's own disposition must not override typed invalid input as OBSERVE."""
    error = error_type(code, "deterministic input")
    assert disposition(error) is Disposition.FAIL
