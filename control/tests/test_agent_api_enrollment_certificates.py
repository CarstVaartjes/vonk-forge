"""Agent API: enrollment certificates."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from pathlib import Path
from typing import SupportsIndex, overload

import pytest
from sqlalchemy import select
from starlette.types import Message

from vonk_agent_protocol.agent_operations import AgentOperation as OperationKind
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_api.authority import RESULT_REFUSALS
from vonk_control.agent_api.common import (
    AgentApiServices,
    EnrollmentRateLimiter,
)
from vonk_control.api.application import create_app
from vonk_control.enrollment.body import _bounded_enrollment_body
from vonk_control.enrollment.validation import _decode_token, _digest
from vonk_control.evidence_repeats import repeating_evidence
from vonk_control.failures.evidence import FailureEvidenceService
from vonk_control.models.fleet import AgentCertificate, AgentEnrollmentGrant
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.recovery_policy import RECOVERY

from .agent_api_support import (
    NODE_A,
    NODE_C,
    STOP_PAYLOAD,
    Jobs,
    _csr_for,
    agent_headers,
    asgi_post,
    assert_corrected_enrollment_succeeds,
    enrollment_grant,
    parent,
    valid_enrollment_body,
)
from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import canonical_start_payload, distribution_payload


class ChunkedEnrollmentRequest:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.received = 0

    async def stream(self):
        for chunk in self.chunks:
            self.received += 1
            yield chunk


class CopyBoundedChunk(bytes):
    def __new__(cls, value: bytes):
        instance = super().__new__(cls, value)
        instance.largest_slice = 0
        return instance

    @overload
    def __getitem__(self, key: SupportsIndex, /) -> int: ...

    @overload
    def __getitem__(self, key: slice, /) -> bytes: ...

    def __getitem__(self, key: SupportsIndex | slice, /) -> int | bytes:
        if isinstance(key, slice):
            start = key.start or 0
            stop = len(self) if key.stop is None else key.stop
            self.largest_slice = max(self.largest_slice, 0, stop - start)
        return super().__getitem__(key)

    def __radd__(self, _other):
        raise AssertionError("an incoming ASGI chunk must never be concatenated whole")


def assert_grant_available(services: AgentApiServices, token: str) -> None:

    assert services.enrollment is not None
    with services.enrollment._sessions() as session:
        grant = session.scalar(
            select(AgentEnrollmentGrant).where(
                AgentEnrollmentGrant.token_digest == _digest(_decode_token(token))
            )
        )
        assert grant is not None
        assert grant.consumed_at is None


def assert_grant_unconsumed(services: AgentApiServices, token: str) -> None:

    with services.sessions() as session:
        grant = session.scalar(
            select(AgentEnrollmentGrant).where(
                AgentEnrollmentGrant.token_digest == _digest(_decode_token(token))
            )
        )
        assert grant is not None
        assert grant.consumed_at is None


def test_staged_certificate_can_only_activate_and_activation_is_idempotent_after_response_loss(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    with services.enrollment._sessions.begin() as session:
        source = session.get(AgentCertificate, "serial-a")
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    csr = _csr_for(NODE_A)
    first = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    replay = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    issued = first.json()
    staged_headers = agent_headers(NODE_A, issued["serial"])
    staged_headers["x-vonk-agent-fingerprint"] = issued["fingerprint"]

    assert client.post("/agent/claim", headers=staged_headers).status_code == 401
    assert (
        client.post(
            "/agent/heartbeat", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/result", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/renew",
            headers=staged_headers,
            json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/agent/artifacts/" + "a" * 64,
            headers=staged_headers,
        ).status_code
        == 401
    )

    activation = {"node_id": NODE_A, "generation": issued["generation"]}
    assert (
        client.post(
            "/agent/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/agent/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post("/agent/claim", headers=agent_headers(NODE_A, "101")).status_code
        == 401
    )
    assert client.post("/agent/claim", headers=staged_headers).status_code == 204
    with services.sessions() as session:
        old = session.get(AgentCertificate, "101")
        new = session.get(AgentCertificate, issued["serial"])
        assert old is not None
        assert old.state == "revoked"
        assert old.revoked_at is not None
        assert new is not None
        assert new.state == "active"
        assert new.revoked_at is None


@pytest.mark.parametrize("with_diagnostics", [False, True])
def test_failed_result_preserves_canonical_evidence_and_maps_parent_reason(
    agent_system,
    with_diagnostics,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    claim = claimed.json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": {
            "status": "failed",
            "error_code": "stop_failed",
            "failure_kind": "temporary-dependency",
        },
    }
    if with_diagnostics:
        fixture = (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
        )
        diagnostics = json.loads(fixture.read_text())
        diagnostics["stderr"]["text"] = (
            "Authorization: Bearer should-never-persist\n/proc: permission denied"
        )
        result["result"]["diagnostics"] = diagnostics

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        parent_job = session.get(
            Job, fenced_operation(services.sessions, claim["fence"]).parent_job_id
        )
        assert attempt.result["status"] == "failed"
        assert attempt.result["error_code"] == result["result"]["error_code"]
        if with_diagnostics:
            typed = FailureDiagnostics.model_validate(attempt.result["diagnostics"])
            assert "/proc: permission denied" in typed.stderr.text
            assert "should-never-persist" not in typed.model_dump_json()
        assert parent_job is not None
        assert parent_job.state == LifecycleState.QUEUED
        assert parent_job.status_reason is not None
        assert parent_job.status_reason.startswith(attempt.result["error_code"])
        operation = session.get(AgentOperation, attempt.operation_id)
        assert operation is not None
        assert operation.next_action_at is not None
        clock.now = operation.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    if with_diagnostics:
        evidence = FailureEvidenceService(services.sessions, clock=clock)
        fenced = fenced_attempt(services.sessions, claim["fence"])
        bundle = evidence.read(fenced.operation_id, fenced.attempt)
        assert "should-never-persist" not in bundle.model_dump_json()
        assert "/proc: permission denied" in bundle.diagnostics.stderr.text
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    fresh = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert fresh.status_code == 200
    assert fresh.json()["fence"] != claim["fence"]


def test_a_refused_start_failure_naming_host_paths_is_accepted_on_its_retry(
    agent_system,
) -> None:
    """Replays the body live Sparks retained after ``422 filesystem path values``.

    The agent keeps a refused result and offers it again; the Controller alone
    must accept it, its core intact and the paths redacted in its prose.
    """

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.start",
        "a" * 64,
        canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30)),
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    fence = claimed.json()["fence"]
    vector = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors"
            / "recipe-start-failed-host-paths-v1.json"
        ).read_text()
    )

    response = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=vector | {"fence": fence},
    )

    assert response.status_code == 204, response.text
    with services.sessions() as session:
        attempt = session.query(AgentOperationAttempt).filter_by(fence=fence).one()
        assert attempt.result["status"] == "failed"
        assert attempt.result["error_code"] == "recipe_start_failed"
        assert "FileNotFoundError" in attempt.result["reason"]
        assert "/opt/vonk" not in attempt.result["reason"]


def test_a_result_refused_on_every_delivery_is_shown_until_one_is_accepted(
    agent_system,
) -> None:
    """Catches a Spark whose every result is refused before the route runs, unseen."""

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    fence = claimed.json()["fence"]
    refused = {
        "fence": fence,
        "state": "failed",
        "result": {"status": "failed", "error_code": "stop/failed"},
    }

    def shown(later: timedelta = timedelta()) -> tuple[str, ...]:
        with services.sessions() as session:
            return repeating_evidence(session, [NODE_A], clock.now + later).get(
                NODE_A, ()
            )

    for _ in range(RECOVERY.max_failures):
        response = client.post(
            "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=refused
        )
        assert response.status_code == 422
    (warning,) = shown()
    assert RESULT_REFUSALS in warning
    assert "controller.invalid_request" in warning
    # Weeks later the same record is old news and is no longer shown.
    assert shown(timedelta(days=14)) == ()

    accepted = refused | {"result": {"status": "failed", "error_code": "stop_failed"}}
    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=accepted
    )
    assert response.status_code == 204
    assert shown() == ()


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        # The accepted boundary is the longest stable code the shared rule
        # allows; a shorter conventional code is the same path.
        ({"status": "failed", "error_code": "a" * 64}, 204),
        ({"status": "failed", "error_code": "a" * 65}, 422),
        ({"status": "failed", "error_code": "workload.host_memory_exhausted"}, 204),
        ({"status": "failed", "error_code": "stop failed"}, 422),
        ({"status": "failed", "error_code": "1stop_failed"}, 422),
        ({"status": "failed", "error_code": "Stop_failed"}, 422),
        # A failed envelope that omits its failed status cannot identify the
        # failure and is refused by the same contextual rule.
        ({"error_code": "stop_failed"}, 422),
    ],
)
def test_failed_result_error_code_obeys_the_shared_contract_rule(
    agent_system, failure, expected_status
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    claim = claimed.json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": failure,
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == expected_status


def test_invalid_failed_results_preserve_the_attempt_and_valid_completion_allows_fresh(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)} | {"state": "failed"}

    malformed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"result": {"status": "failed", "error_code": "Stop_failed"}},
    )

    assert malformed.status_code == 422

    unnamed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"result": {"error_code": "stop_failed"}},
    )

    assert unnamed.status_code == 422
    with services.sessions() as session:
        attempt = fenced_attempt(services.sessions, claim["fence"])
        stored = session.get(AgentOperationAttempt, attempt.id)
        assert stored is not None
        assert stored.result is None
        assert stored.state == LifecycleState.RUNNING
    completed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"fence": claim["fence"], "state": LifecycleState.SUCCEEDED, "result": {}},
    )
    assert completed.status_code == 204
    fresh = services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP.value,
        "a" * 64,
        STOP_PAYLOAD,
    )
    next_claim = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert next_claim.status_code == 200
    assert (
        fenced_operation(services.sessions, next_claim.json()["fence"]).id == fresh.id
    )


# The failure envelope spark-3542's agent retained for the artifact
# distribution whose report the Controller refused.  ``failure_kind`` is a
# declared string enum and the diagnostics list sits on its declared bound.
INCIDENT_DISTRIBUTION_FAILURE = {
    "status": "failed",
    "error_code": "artifact_distribution_failed",
    "failure_kind": "temporary-dependency",
    "reason": "Controller distribution could not be verified and retained",
    "diagnostics": {
        "schema_version": 1,
        "category": "runtime",
        "collected_at": "2026-09-16T23:29:02.483132899+00:00",
        "phase": "artifact.distribution.v1",
        "stdout": {
            "text": "",
            "truncated": False,
            "dropped_bytes": 0,
            "dropped_lines": 0,
        },
        "stderr": {
            "text": "",
            "truncated": False,
            "dropped_bytes": 0,
            "dropped_lines": 0,
        },
        "collector_errors": [],
        "preflight": [],
        "versions": [
            {"name": "agent", "value": "0.1.1"},
            {"name": "kernel", "value": "6.17.0-1031-nvidia"},
            {"name": "podman", "value": "podman version 4.9.3"},
        ],
        "storage": [
            {"name": "data-root", "value": "/var/lib/vonk-forge-agent"},
            {"name": "free-bytes", "value": "2483534802944"},
        ],
        "sandbox": [
            {"name": "Uid", "value": "128\t128\t128\t128"},
            {"name": "Gid", "value": "127\t127\t127\t127"},
            {"name": "CapEff", "value": "0000000000000000"},
            {"name": "NoNewPrivs", "value": "0"},
            {"name": "Seccomp", "value": "2"},
            {"name": "User", "value": "vonk-agent"},
            {
                "name": "ReadWritePaths",
                "value": "/var/lib/vonk-forge-agent /var/lib/vonk-forge/incoming"
                " /run/vonk-forge-agent",
            },
            {
                "name": "ReadOnlyPaths",
                "value": "/etc/vonk-forge-agent /usr/lib/vonk-forge",
            },
            {"name": "PrivateTmp", "value": "yes"},
            {"name": "ProtectSystem", "value": "strict"},
            {"name": "NoNewPrivileges", "value": "no"},
            {"name": "RestrictNamespaces", "value": "no"},
        ],
    },
}


def test_declared_failure_kind_survives_agent_result_ingress(agent_system) -> None:
    """A truthful ``failure_kind`` must not be refused as an invalid request.

    ``failure_kind`` is a string enum in the published wire schema, and the
    agent sends the member value.  A body model that resolves an enum only from
    an enum instance rejects that value, so every replay of a genuine failure
    was answered 422 -- which is what left spark-3542's agent looping until
    systemd exhausted its restart limit -- and the retained evidence was never
    consumed.  Replay the retained envelope through the real ingress and require
    both acceptance and the preserved receipt.
    """

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.ARTIFACT_DISTRIBUTION,
        "a" * 64,
        distribution_payload(),
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": INCIDENT_DISTRIBUTION_FAILURE,
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=envelope
    )

    assert response.status_code == 204, response.text
    with services.sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim["fence"]
            )
        )
        assert attempt is not None
        assert attempt.state == "failed"
        assert attempt.result["failure_kind"] == "temporary-dependency"
        assert (
            attempt.result["error_code"] == INCIDENT_DISTRIBUTION_FAILURE["error_code"]
        )
        # The bound diagnostics survive sanitization rather than being dropped.
        assert attempt.result["diagnostics"]["phase"] == "artifact.distribution.v1"
        assert len(attempt.result["diagnostics"]["sandbox"]) == 12
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.ARTIFACT_DISTRIBUTION,
        "a" * 64,
        distribution_payload(),
    )
    fresh = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert fresh.status_code == 200
    assert fresh.json()["fence"] != claim["fence"]


def test_boundary_failures_record_a_correlated_operator_reason(agent_system) -> None:
    """A refused result or heartbeat must leave a bounded, correlated reason.

    The claim path already explained its refusals, but a refused heartbeat or
    result persisted nothing, so an operator saw an operation that stopped
    progressing with no cause at all.
    """

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)}

    rejected = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"state": "succeeded", "result": {"installed_bytes": 0}},
    )

    assert rejected.status_code == 422
    with services.sessions() as session:
        fenced = fenced_operation(services.sessions, claim["fence"])
        operation = session.get(AgentOperation, fenced.id)
        job = session.get(Job, fenced.parent_job_id)
        assert operation is not None
        assert operation.status_reason is not None
        assert job is not None
        assert "installed_bytes" in (job.status_reason or "")
        # The same bytes are refused on every delivery: the attempt is settled
        # failed with the refusal as its evidence, never left to lapse into a
        # reissue of the same work.
        assert operation.state == LifecycleState.FAILED
        assert operation.next_action_at is None

    attempt = fenced_attempt(services.sessions, claim["fence"])
    bundle = FailureEvidenceService(services.sessions, clock=clock).read(
        fenced.id, attempt.attempt
    )
    assert "installed_bytes" in bundle.model_dump_json()
    assert (
        client.post(
            "/agent/result",
            headers=agent_headers(NODE_A, "serial-a"),
            json=envelope | {"state": "succeeded", "result": {"installed_bytes": 0}},
        ).status_code
        == 422
    )
    with services.sessions() as session:
        assert session.get(AgentOperation, fenced.id).state == LifecycleState.FAILED

    clock.now += timedelta(seconds=61)
    heartbeat = client.post(
        "/agent/heartbeat",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"progress": {"phase": "stopping"}},
    )

    assert heartbeat.status_code == 409
    with services.sessions() as session:
        operation = session.get(
            AgentOperation, fenced_operation(services.sessions, claim["fence"]).id
        )
        assert operation is not None
        assert operation.status_reason is not None


def test_a_concluded_operation_never_acquires_a_boundary_refusal_note(
    agent_system,
) -> None:
    """A refusal must never be attached to an outcome that already concluded.

    Wrong implementation: ``_write_refusal_note`` keyed only on the submission's
    correlating attempt and fence, so a late heartbeat for an operation that had
    already succeeded wrote ``heartbeat refused: stale-attempt`` onto the
    succeeded operation and its succeeded parent job.  The job then reported a
    refusal while its owning state said ``succeeded`` -- a success that reads to
    an operator as a refusal of work that already completed.
    """

    client, services, _, clock = agent_system
    job = parent(services.sessions, clock)
    operation = services.operations.enqueue(
        job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)}
    completed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"state": "succeeded", "result": {}},
    )
    assert completed.status_code == 204, completed.text
    with services.sessions() as session:
        concluded = session.get(AgentOperation, operation.id)
        succeeded = session.get(Job, job.id)
        assert concluded is not None
        assert concluded.state == "succeeded"
        assert succeeded is not None
        assert succeeded.state == "succeeded"

    stale = client.post(
        "/agent/heartbeat",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"progress": {"phase": "stopping"}},
    )
    assert stale.status_code == 409, stale.text
    with services.sessions() as session:
        concluded = session.get(AgentOperation, operation.id)
        succeeded = session.get(Job, job.id)
        assert concluded is not None
        assert concluded.state == "succeeded"
        assert succeeded is not None
        assert succeeded.state == "succeeded"
        # The parent job is the owner surface the operator reads; it must not
        # carry a refusal for work that already reached its outcome.
        assert succeeded.status_reason is None
        assert concluded.status_reason is None


def test_a_concluded_orders_refused_result_never_annotates_its_waiting_parent(
    agent_system, caplog
) -> None:
    """A parent's reason describes its current state, not a settled sibling.

    Wrong implementation (production 2026-10-17): a sequential agent upgrade
    settled its first order, queued the next, then refused a late result for
    the first order's attempt 1.  The order was left unannotated, but its
    still-queued parent showed ``result refused: stale-attempt (attempt=1)``,
    so a healthy wait for the next Spark read as a failure.
    """

    client, services, _, clock = agent_system
    caplog.set_level("INFO")
    job = parent(services.sessions, clock)
    for _ in range(2):
        services.operations.enqueue(
            job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
        )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)}
    completed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"state": "succeeded", "result": {}},
    )
    assert completed.status_code == 204, completed.text

    late = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope
        | {
            "state": "failed",
            "result": {"status": "failed", "error_code": "stop_failed"},
        },
    )

    assert late.status_code == 409, late.text
    with services.sessions() as session:
        waiting = session.get(Job, job.id)
        settled = session.get(
            AgentOperation, fenced_operation(services.sessions, claim["fence"]).id
        )
        assert waiting is not None
        assert waiting.state not in {"succeeded", "failed"}
        assert "refused" not in (waiting.status_reason or "")
        assert settled is not None
        assert settled.state == "succeeded"
    # The evidence stays: the order's own accepted outcome, and the refusal
    # in the Controller's one line per refused agent request.
    assert fenced_attempt(services.sessions, claim["fence"]).state == "succeeded"
    assert any(
        "agent.request_rejected" in record.getMessage()
        and '"http_status":409' in record.getMessage()
        for record in caplog.records
    )


def test_invalid_failed_result_is_not_reported_as_an_acknowledged_stale_attempt(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": {"unexpected": "unstructured failure"},
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 422
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        assert attempt.state == "running"
        assert attempt.result is None


def test_recipe_job_failure_uses_its_typed_exit_result_at_authenticated_ingress(
    agent_system,
):

    client, services, _, clock = agent_system
    vectors = (
        Path(__file__).parents[2] / "agent_protocol/src/vonk_agent_protocol/vectors"
    )
    request = json.loads((vectors / "recipe-job-run-claim-v1.json").read_text())[
        "payload"
    ]
    job_result = json.loads((vectors / "recipe-job-run-result-v1.json").read_text())[
        "result"
    ]
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.job.run.v1",
        "a" * 64,
        request,
    )
    claim_response = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    )
    assert claim_response.status_code == 200
    claim = claim_response.json()
    result = {key: claim[key] for key in ("fence",)}
    result.update(
        state="failed", result=dict(job_result, exit_code=1, reason="runtime failed")
    )
    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )
    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        assert attempt.state == "failed"
        assert attempt.result["exit_code"] == 1


def test_agent_validation_errors_are_canonical_json(agent_system) -> None:
    client, _, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"wait_seconds": -1},
    )

    assert response.status_code == 422
    assert response.content == canonical_message(response.json())


def test_claim_endpoint_long_poll_wakes_when_work_is_enqueued(agent_system) -> None:
    client, services, _, clock = agent_system
    parent_job = parent(services.sessions, clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        # A long wait with generous bounds: shared runners can stall a thread
        # for seconds, so correctness must not depend on wall-clock timing.
        waiting = pool.submit(
            client.post,
            "/agent/claim",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"wait_seconds": 30},
        )
        # Synchronize on the observable event instead of sleeping: the claim
        # is parked on the availability condition once it has a waiter.
        deadline = time.monotonic() + 20
        while not services.operations._available._waiters:
            assert not waiting.done(), "claim returned before work existed"
            assert time.monotonic() < deadline, "claim never began waiting"
            time.sleep(0.005)
        operation = services.operations.enqueue(
            parent_job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
        )
        response = waiting.result(timeout=20)

    assert response.status_code == 200
    assert (
        fenced_operation(services.sessions, response.json()["fence"]).id == operation.id
    )


def test_enrollment_rate_limit_rejects_before_reading_request_body(
    agent_system,
) -> None:
    _, services, codec, _ = agent_system
    limiter = EnrollmentRateLimiter(maximum=1, window_seconds=60, clock=lambda: 0.0)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        agent=services,
        enrollment_rate_limiter=limiter,
    )
    assert (
        asgi_post(
            app, "/agent/enroll", valid_enrollment_body(enrollment_grant(services))
        )[0]
        == 200
    )
    sent: list[Message] = []
    reads = 0

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"never-read", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/enroll",
        "raw_path": b"/agent/enroll",
        "query_string": b"",
        "headers": ((b"content-type", b"application/json"),),
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=10))

    assert (
        next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]
        == 429
    )
    assert reads == 0


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "non-object",
        "ambiguous",
        "content-type",
        "evidence",
        "unicode-csr",
        "oversized-csr",
        "utf8",
    ],
)
def test_malformed_enrollment_preserves_grant_then_corrected_request_succeeds(
    agent_system, damage
):
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    valid = valid_enrollment_body(token)
    body = json.loads(valid)
    content_type = "application/json"
    if damage == "json":
        raw = f'{{"grant_token":"{token}",]'.encode()
    elif damage == "non-object":
        raw = f'[{{"grant_token":"{token}"}}]'.encode()
    elif damage == "ambiguous":
        raw = f'{{"grant_token":"{token}","grant_token":"{token}"}}'.encode()
    elif damage == "content-type":
        raw = valid
        content_type = "text/plain"
    elif damage == "utf8":
        raw = (
            f'{{"grant_token":"{token}","csr":"'.encode()
            + bytes([255])
            + bytes([34, 125])
        )
    else:
        if damage == "evidence":
            body["evidence"] = []
        elif damage == "unicode-csr":
            body["csr"] = "é"
        else:
            body["csr"] = "x" * (16 * 1024 + 1)
        raw = json.dumps(body).encode()
    status, _ = asgi_post(client.app, "/agent/enroll", raw, content_type=content_type)
    assert 400 <= status < 500
    assert_grant_available(services, token)
    status, response = asgi_post(client.app, "/agent/enroll", valid)
    assert status == 200
    assert json.loads(response)["node_id"] == NODE_C


def test_duplicate_enrollment_grants_preserve_unicode_escaped_token_values(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    first = enrollment_grant(services)
    second = enrollment_grant(services)
    escaped_second = "".join(f"\\u{ord(character):04x}" for character in second)
    raw = (
        f'{{"grant_token":"{first}","gr\\u0061nt_token":"{escaped_second}"}}'
    ).encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422
    assert_grant_unconsumed(services, first)
    assert_grant_unconsumed(services, second)


def test_normal_enrollment_object_still_succeeds(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, response = asgi_post(
        client.app,
        "/agent/enroll",
        valid_enrollment_body(token),
    )

    assert status_code == 200
    assert json.loads(response)["node_id"] == NODE_C
    assert "certificate_pem" in json.loads(response)


def test_oversized_enrollment_preserves_split_discovery_prefix(agent_system) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    first = b" " * 1000 + b'{"grant_to'
    second = b'ken":"' + token.encode("ascii") + b'","padding":"' + b"x" * (64 * 1024)
    request = ChunkedEnrollmentRequest(first, second, b"must-not-be-received")

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; bounded body and corrected grant below
        asyncio.run(_bounded_enrollment_body(request))  # type: ignore[arg-type]

    assert request.received == 2
    assert_corrected_enrollment_succeeds(services, token)


def test_one_huge_enrollment_chunk_is_only_copied_through_fixed_prefix(
    agent_system,
) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    huge = CopyBoundedChunk(
        b'{"grant_token":"'
        + token.encode("ascii")
        + b'","padding":"'
        + b"x" * (1024 * 1024)
    )
    request = ChunkedEnrollmentRequest(huge, b"must-not-be-received")

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; bounded body and corrected grant below
        asyncio.run(_bounded_enrollment_body(request))  # type: ignore[arg-type]

    assert request.received == 1
    assert huge.largest_slice <= 2048
    assert_corrected_enrollment_succeeds(services, token)


@pytest.mark.parametrize(
    "raw",
    [b"[1]", b"[]", b'"scalar"', b"0", b"true", b"false", b"null"],
    ids=("array", "empty-array", "string", "number", "true", "false", "null"),
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_enrollment_rejects_non_object_json_without_server_error(
    agent_system, raw: bytes
) -> None:
    client, _, _, _ = agent_system

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422


def test_non_object_enrollment_preserves_identifiable_nested_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    raw = f'[{{"grant_token":"{token}"}}]'.encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422
    assert_corrected_enrollment_succeeds(services, token)


def test_invalid_evidence_preserves_every_discovered_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    effective = enrollment_grant(services)
    nested = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(effective))
    body["evidence"]["extra"] = {"grant_token": nested}

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        json.dumps(body).encode("utf-8"),
    )

    assert status_code == 422
    assert_grant_unconsumed(services, effective)
    assert_grant_unconsumed(services, nested)
