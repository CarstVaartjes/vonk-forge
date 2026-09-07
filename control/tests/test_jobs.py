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
from vonk_control.models import Base


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
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    ), clock


def test_workers_cannot_claim_same_job(service) -> None:
    jobs, _ = service
    job = jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {"safe": True})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda index: jobs.claim(f"worker-{index}", 30), range(4)))
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


def test_job_list_keyset_pages_reach_every_job_in_stable_order(service) -> None:
    jobs, clock = service
    expected: set[str] = set()
    for index in range(23):
        expected.add(
            jobs.enqueue(
                "probe", "admin", "a" * 40, [f"spk_{index:032x}"], {}
            ).id
        )
        if index % 3 == 0:
            clock.now += timedelta(seconds=1)

    found: list[str] = []
    cursor = None
    while True:
        page, cursor, total = jobs.list_page(limit=7, cursor=cursor)
        found.extend(job.id for job in page)
        assert total == 23
        if cursor is None:
            break

    assert len(found) == len(set(found)) == 23
    assert set(found) == expected
    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=7, cursor="not-a-cursor")


def test_job_list_cursor_is_authenticated_and_filter_bound(service) -> None:
    jobs, _clock = service
    for index in range(3):
        jobs.enqueue(
            "probe",
            "admin",
            "a" * 40,
            [f"spk_{index:032x}"],
            {},
        )

    _page, cursor, _total = jobs.list_page(limit=1, status="queued")
    assert cursor is not None and len(cursor) <= 512

    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=1, cursor=cursor[:-1] + replacement, status="queued")
    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=1, cursor=cursor, status="running")
    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=1, cursor="v1.e30.A" * 300, status="queued")


def test_job_list_keyset_cursor_excludes_concurrent_newer_insert(service) -> None:
    jobs, clock = service
    initial = [
        jobs.enqueue("probe", "admin", "a" * 64, [f"spk_{index:032x}"], {})
        for index in range(3)
    ]
    first, cursor, total = jobs.list_page(limit=2)
    assert cursor is not None and total == 3

    clock.now += timedelta(seconds=1)
    inserted = jobs.enqueue(
        "probe", "admin", "a" * 40, ["spk_" + "f" * 32], {}
    )
    second, next_cursor, updated_total = jobs.list_page(limit=2, cursor=cursor)

    assert {job.id for job in second} == {
        job.id for job in initial
    } - {job.id for job in first}
    assert inserted.id not in {job.id for job in (*first, *second)}
    assert next_cursor is None
    assert updated_total == 4


def test_job_list_rejects_syntactically_valid_cursor_forged_with_other_key(
    service,
) -> None:
    jobs, clock = service
    for index in range(2):
        jobs.enqueue("probe", "admin", "a" * 40, [f"spk_{index:032x}"], {})
    forged_service = JobService(
        jobs._sessions,
        clock=clock,
        cursors=TokenCodec(b"z" * 32).cursor_codec(),
    )
    _page, forged, _total = forged_service.list_page(limit=1)
    assert forged is not None

    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=1, cursor=forged)


def test_claim_carries_commit_and_targets_to_the_worker(service) -> None:
    jobs, _ = service
    jobs.enqueue("probe", "admin", "a" * 64, ["spk_a", "spk_b"], {})

    attempt = jobs.claim("worker", 30)

    assert attempt is not None
    assert attempt.authority_revision == "a" * 64
    assert attempt.targets == ("spk_a", "spk_b")


def test_stale_attempt_cannot_publish_success_after_lease_reclaim(service) -> None:
    jobs, clock = service
    jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {})
    first = jobs.claim("worker-1", 10)
    assert first is not None
    clock.now += timedelta(seconds=11)
    second = jobs.claim("worker-2", 30)
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


def test_matching_fence_can_heartbeat_wait_and_fail(service) -> None:
    jobs, _ = service
    jobs.enqueue("install", "operator", "abc", ["spk_1"], {})
    attempt = jobs.claim("worker", 10)
    assert attempt is not None
    renewed = jobs.heartbeat(attempt, 20)
    assert renewed.lease_deadline > attempt.lease_deadline
    jobs.wait_for_operator(renewed, "confirm console fingerprint")
    assert jobs.get(renewed.job_id).state == "waiting-for-operator"

    jobs.resume(renewed.job_id)
    retry = jobs.claim("worker", 10)
    assert retry is not None
    jobs.fail(retry, "bounded failure")
    assert jobs.get(retry.job_id).state == "failed"


@pytest.mark.parametrize("kind", [
    "agent-upgrade", "artifact-distribution", "recipe.run-switch.v2", "recipe.stop.v2",
])
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

    attempt = jobs.claim("worker", 10)

    assert attempt is not None and attempt.job_id == install.id
    stored = jobs.get(upgrade.id)
    assert stored.state == "queued"
    assert stored.current_attempt == 0
    assert stored.payload == {"immutable": "upgrade-plan"}


def test_concurrent_operator_resume_has_one_winner(service) -> None:
    jobs, _ = service
    jobs.enqueue("install", "operator", "abc", ["spk_1"], {})
    attempt = jobs.claim("worker", 10)
    assert attempt is not None
    jobs.wait_for_operator(attempt, "confirm console fingerprint")

    def resume() -> str:
        try:
            jobs.resume(attempt.job_id)
            return "won"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: resume(), range(8)))

    assert outcomes.count("won") == 1
    assert outcomes.count("conflict") == 7
