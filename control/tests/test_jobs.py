from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.auth import TokenCodec
from vonk_control.jobs import JobService, StaleAttempt
from vonk_control.models import Base, Job
from vonk_control.operation_api import JobProgress, OperationPage, job_response
from vonk_control.stored_json import write_guard_mode


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    return JobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=clock,
    ), clock


def test_workers_cannot_claim_same_job(service) -> None:
    jobs, _ = service
    job = jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {"safe": True})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(
                lambda index: jobs.claim(f"worker-{index}", 30, kinds=("probe",)),
                range(4),
            )
        )
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].job_id == job.id


def test_repeated_request_key_replays_one_durable_job(service) -> None:
    jobs, _ = service
    first = jobs.enqueue(
        "install",
        "operator",
        "authority",
        ["spk_1"],
        {"plan_digest": "a" * 64},
        request_id="request-key",
    )
    replay = jobs.enqueue(
        "install",
        "operator",
        "authority",
        ["spk_1"],
        {"plan_digest": "a" * 64},
        request_id="request-key",
    )
    assert replay.id == first.id
    with pytest.raises(ValueError, match="already used differently"):
        jobs.enqueue(
            "install",
            "different-actor",
            "authority",
            ["spk_1"],
            {"plan_digest": "b" * 64},
            request_id="request-key",
        )


def test_claim_carries_commit_and_targets_to_the_worker(service) -> None:
    jobs, _ = service
    jobs.enqueue("probe", "admin", "a" * 64, ["spk_a", "spk_b"], {})

    attempt = jobs.claim("worker", 30, kinds=("probe",))

    assert attempt is not None
    assert attempt.authority_revision == "a" * 64
    assert attempt.targets == ("spk_a", "spk_b")


def test_stale_attempt_cannot_publish_success_after_lease_reclaim(service) -> None:
    jobs, clock = service
    jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {})
    first = jobs.claim("worker-1", 10, kinds=("probe",))
    assert first is not None
    clock.now += timedelta(seconds=11)
    second = jobs.claim("worker-2", 30, kinds=("probe",))
    assert second is not None and second.fence != first.fence
    with pytest.raises(StaleAttempt):
        jobs.succeed(first, {"wrong": True})
    jobs.succeed(second, {"ok": True})
    assert jobs.get(second.job_id).state == "succeeded"


def test_payload_is_bounded_and_rejects_credential_fields(service) -> None:
    jobs, _ = service
    with pytest.raises(ValueError, match="sensitive"):
        jobs.enqueue("probe", "admin", "abc", [], {"password": "no"})
    with pytest.raises(ValueError, match="large"):
        jobs.enqueue("probe", "admin", "abc", [], {"value": "x" * 70_000})
    with pytest.raises(TypeError, match="keys"):
        jobs.enqueue("probe", "admin", "abc", [], {1: "not-a-string-key"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_job_payload_and_result_reject_nonfinite_json_numbers(service, value) -> None:
    jobs, _ = service
    with pytest.raises(ValueError):
        jobs.enqueue("probe", "admin", "abc", [], {"nested": [value]})

    job = jobs.enqueue("probe", "admin", "abc", [], {})
    attempt = jobs.claim("worker", 30, kinds=("probe",))
    assert attempt is not None and attempt.job_id == job.id
    with pytest.raises(ValueError):
        jobs.succeed(attempt, {"nested": [value]})
    assert jobs.get(job.id).state == "running"


def test_job_json_values_targets_and_projection_survive_store_load(service) -> None:
    jobs, _ = service
    payload = {
        "empty": "",
        "false": False,
        "zero": 0,
        "none": None,
        "engine_extension": {"enabled": False, "values": []},
    }
    result = {
        "empty": "",
        "false": False,
        "zero": 0,
        "none": None,
        "engine_extension": {"enabled": False, "values": []},
    }
    job = jobs.enqueue("probe", "admin", "abc", ("target-a", "target-b"), payload)
    loaded = jobs.get(job.id)
    assert loaded.payload == payload
    assert loaded.targets == ["target-a", "target-b"]
    attempt = jobs.claim("worker", 30, kinds=("probe",))
    assert attempt is not None
    assert attempt.payload == payload
    assert attempt.targets == ("target-a", "target-b")
    jobs.succeed(attempt, result)
    loaded = jobs.get(job.id)
    assert loaded.result == result
    projected = job_response(
        loaded,
        OperationPage(
            items=[],
            next_cursor=None,
            progress=JobProgress(completed=0, failed=0, running=0, total=0),
        ),
        target_cursor=0,
        limit=100,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    assert projected.targets == ["target-a", "target-b"]


@pytest.mark.parametrize("targets", [[1], ["target", 1], "target", None])
def test_job_targets_reject_non_string_json_members_on_enqueue(
    service, targets
) -> None:
    jobs, _ = service
    with pytest.raises(ValueError, match="job targets"):
        jobs.enqueue("probe", "admin", "abc", targets, {})


def test_job_targets_reject_malformed_persisted_json_on_read(service) -> None:
    jobs, _ = service
    job = jobs.enqueue("probe", "admin", "abc", ["target"], {})
    with write_guard_mode(strict=False), jobs._sessions.begin() as session:
        row = session.get(Job, job.id)
        assert row is not None
        row.targets = [1]
    with pytest.raises(ValueError, match="job targets"):
        jobs.get(job.id)


@pytest.mark.parametrize(
    "payload",
    [
        {"tokens_per_minute": "nested-secret"},
        {"safe": {"tokens_per_minute": 10_000}},
        {
            "routes": {
                "chat": {
                    "quota": {
                        "requests_per_minute": 30,
                        "tokens_per_minute": "nested-secret",
                    }
                }
            }
        },
    ],
)
def test_token_named_fields_outside_validated_route_quota_are_sensitive(
    service, payload: dict[str, object]
) -> None:
    jobs, _ = service

    with pytest.raises(ValueError, match="sensitive"):
        jobs.enqueue("reconcile", "admin", "abc", ["spk_1"], payload)


def test_exact_bounded_reconciliation_route_quota_is_accepted(service) -> None:
    jobs, _ = service
    quota = {"requests_per_minute": 30, "tokens_per_minute": 10_000}
    node_id = "spk_00000000000000000000000000000001"
    payload = {
        "routes": {
            "chat": {
                "workload_id": "model",
                "nodes": [node_id],
                "entrypoint_node_id": node_id,
                "scheme": "http",
                "port": 8000,
                "path": "/v1",
                "quota": quota,
                "quota_digest": hashlib.sha256(canonical_message(quota)).hexdigest(),
            }
        }
    }

    accepted = jobs.enqueue(
        "reconcile",
        "admin",
        "abc",
        [node_id],
        payload,
    )
    assert accepted.payload == payload


def test_matching_fence_can_heartbeat_and_fail(service) -> None:
    jobs, _ = service
    jobs.enqueue("install", "operator", "abc", ["spk_1"], {})
    attempt = jobs.claim("worker", 10, kinds=("install",))
    assert attempt is not None
    renewed = jobs.heartbeat(attempt, 20)
    assert renewed.lease_deadline > attempt.lease_deadline
    jobs.fail(renewed, "bounded failure")
    assert jobs.get(renewed.job_id).state == "failed"


@pytest.mark.parametrize(
    "kind",
    [
        "agent-upgrade",
        "artifact-distribution",
        "recipe.run-switch.v2",
        "recipe.stop.v2",
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_generic_worker_claim_skips_coordinator_owned_jobs(service, kind) -> None:
    jobs, _ = service
    upgrade = jobs.enqueue(
        kind,
        "operator",
        "abc",
        ["spk_1"],
        {"immutable": "upgrade-plan"},
    )
    install = jobs.enqueue("install", "operator", "abc", ["spk_1"], {})

    attempt = jobs.claim("worker", 10, kinds=("install",))

    assert attempt is not None and attempt.job_id == install.id
    stored = jobs.get(upgrade.id)
    assert stored.state == "queued"
    assert stored.current_attempt == 0
    assert stored.payload == {"immutable": "upgrade-plan"}


def test_job_refusals_are_invalid_requests_with_reasons(service) -> None:
    from vonk_agent_protocol import InvalidRequestError, InvalidRequestReason

    jobs, _ = service
    jobs.enqueue("probe", "admin", "abc", ["spk_1"], {"a": 1}, request_id="r1")
    with pytest.raises(InvalidRequestError) as differently:
        jobs.enqueue("probe", "admin", "abc", ["spk_1"], {"a": 2}, request_id="r1")
    assert differently.value.typed_reason is InvalidRequestReason.CONFLICT
    with pytest.raises(InvalidRequestError) as missing:
        jobs.get("absent")
    assert missing.value.typed_reason is InvalidRequestReason.NOT_FOUND
    with pytest.raises(InvalidRequestError) as sensitive:
        jobs.enqueue("probe", "admin", "abc", ["spk_1"], {"token": "x"})
    assert sensitive.value.typed_reason is InvalidRequestReason.MALFORMED
    with pytest.raises(InvalidRequestError) as stale:
        jobs.enqueue_guarded(
            "probe", "admin", "abc", ["spk_1"], {}, authority_check=lambda: False
        )
    assert stale.value.typed_reason is InvalidRequestReason.SUPERSEDED
