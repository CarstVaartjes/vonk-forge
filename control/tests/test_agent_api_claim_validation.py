"""Agent API: claim validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from vonk_agent_protocol.agent_words import (
    RecipeRunDispositionValue,
)
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.state_machines import RunState
from vonk_control import agent_operation_states as aos
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.inventory.items import runtime_value
from vonk_control.models.execution import RecipeRun, RunNode
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperationAttempt
from vonk_control.pki import IssuedCertificate

from .agent_api_support import (
    NODE_A,
    NODE_B,
    NODE_C,
    PACKAGED_RUNTIME_IDENTITY,
    STOP_PAYLOAD,
    _csr_for,
    agent_headers,
    enrollment_grant,
    parent,
    valid_enrollment_body,
)
from .node_contacts import contact_row


@pytest.mark.parametrize("architecture", ["linux-riscv64", True, 7])
def test_claim_api_rejects_noncanonical_runtime_architecture(
    agent_system, architecture: object
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "runtime_identity": {
                "architecture": architecture,
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "c" * 64,
                "semantic_version": "1.2.3",
            },
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert runtime_value(node, lambda identity: identity.architecture) is None


def test_unauthenticated_claim_cannot_change_runtime_architecture(agent_system) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        json={
            "runtime_identity": {
                "architecture": "linux-arm64",
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "semantic_version": "1.2.3",
            },
        },
    )

    assert response.status_code in {401, 403}
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert runtime_value(node, lambda identity: identity.architecture) is None


@pytest.mark.parametrize(
    ("field", "nested"),
    [("future_claim_field", False), ("future_attestation", True)],
)
def test_claim_rejects_unknown_structural_fields(
    agent_system,
    field: str,
    nested: bool,
) -> None:
    client, _services, _, _clock = agent_system
    payload = {
        "protocol_version": 4,
        "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
    }
    if nested:
        payload["runtime_identity"] = {
            **PACKAGED_RUNTIME_IDENTITY,
            field: {"format": "v2"},
        }
    else:
        payload[field] = {"version": 4}

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["protocol_version", "wait_seconds"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_claim_rejects_string_encoded_numeric_fields(
    agent_system,
    field: str,
) -> None:
    client, _services, _, _clock = agent_system
    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "protocol_version": 4,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            field: "30",
        },
    )

    assert response.status_code == 422


def test_authenticated_heartbeat_preserves_claim_advertised_protocol_after_exact_fence_validation(
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
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact_row(node).last_seen_at = None
    clock.now += timedelta(seconds=5)
    progress = {key: claim[key] for key in ("fence",)} | {
        "progress": {"phase": "checking"}
    }

    response = client.post(
        "/agent/heartbeat",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.43",
        },
        json=progress,
    )

    assert response.status_code == 200
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        seen = contact_row(node).last_seen_at
        assert seen is not None
        assert seen.replace(tzinfo=UTC) == clock.now
        assert contact_row(node).protocol_version == 4
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.43"
        observed = contact_row(presence).management_observed_at
        assert observed is not None
        assert observed.replace(tzinfo=UTC) == clock.now


def test_authenticated_result_preserves_claim_advertised_protocol_after_exact_fence_validation(
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
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact_row(node).last_seen_at = None
    clock.now += timedelta(seconds=5)
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "succeeded",
        "result": {},
    }

    response = client.post(
        "/agent/result",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.44",
        },
        json=result,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        seen = contact_row(node).last_seen_at
        assert seen is not None
        assert seen.replace(tzinfo=UTC) == clock.now
        assert contact_row(node).protocol_version == 4
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.44"
        observed = contact_row(presence).management_observed_at
        assert observed is not None
        assert observed.replace(tzinfo=UTC) == clock.now


def test_failed_stop_result_never_writes_health_observation(agent_system) -> None:
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
        "result": {"status": "failed", "error_code": "stop_failed"},
    }

    assert (
        client.post(
            "/agent/result",
            headers=agent_headers(NODE_A, "serial-a"),
            json=result,
        ).status_code
        == 204
    )


def test_untrusted_and_stale_requests_do_not_record_agent_contact(agent_system) -> None:
    client, services, _, clock = agent_system
    untrusted = client.post(
        "/agent/claim",
        json={
            "protocol_version": 4,
            "wait_seconds": 0,
        },
    )
    assert untrusted.status_code == 401
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None

    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a")).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        presence = session.get(AgentNode, NODE_A)
        assert node is not None
        assert presence is not None
        recorded = contact_row(presence).management_observed_at
        assert recorded is not None
        observed_at = recorded.replace(tzinfo=UTC)
        contact_row(node).last_seen_at = None
        contact_row(node).protocol_version = None
    clock.now += timedelta(seconds=5)
    stale = {
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {},
    }

    rejected = client.post(
        "/agent/result",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.43",
        },
        json=stale,
    )

    assert rejected.status_code == 409
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.42"
        observed = contact_row(presence).management_observed_at
        assert observed is not None
        assert observed.replace(tzinfo=UTC) == observed_at


@pytest.mark.parametrize("version", [True, 3, 5])
def test_unsupported_protocol_version_is_rejected_without_recording_contact(
    agent_system, version: object
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "protocol_version": version,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None


@pytest.mark.parametrize("mutation", ["revoked", "retired", "expired", "fingerprint"])
def test_persisted_certificate_state_is_checked_on_every_agent_request(
    agent_system, mutation: str
) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        node = session.get(AgentNode, NODE_A)
        assert certificate is not None
        assert node is not None
        if mutation == "revoked":
            certificate.revoked_at = clock.now
        elif mutation == "retired":
            node.state = "retired"
        elif mutation == "expired":
            certificate.not_after = clock.now
        else:
            certificate.fingerprint = "different"
    assert (
        client.post(
            "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 401
    )


def test_fence_and_cross_node_result_updates_are_denied(agent_system) -> None:
    # A result for another node's fence is an authority edge (403), not a
    # verdict on the request's own bytes.
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
    result = {key: claim[key] for key in ("fence",)}
    foreign = {**result, "state": "succeeded", "result": {}}
    assert (
        client.post(
            "/agent/result", headers=agent_headers(NODE_B, "serial-b"), json=foreign
        ).status_code
        == 403
    )
    stale = {
        **result,
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {},
    }
    assert (
        client.post(
            "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=stale
        ).status_code
        == 409
    )


def test_expired_exact_result_is_retained_as_diagnostic_without_completing_job(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    job = parent(services.sessions, clock)
    services.operations.enqueue(job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD)
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    clock.now += timedelta(seconds=61)
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "succeeded",
        "result": {},
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )
    assert response.status_code == 202
    with services.sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim["fence"]
            )
        )
        assert aos.attempt_lapsed(attempt)
        assert attempt.result == {}
        assert session.get(Job, job.id).state != "succeeded"


def test_public_enrollment_bootstrap_is_canonical_bounded_and_contains_only_public_trust(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    assert services.bootstrap is not None
    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )

    response = client.get("/agent/bootstrap")

    assert response.status_code == 200
    assert response.content == canonical_message(response.json())
    assert response.json() == {
        "ca_fingerprint": services.bootstrap.ca_fingerprint,
        "ca_pem": services.bootstrap.ca_pem,
        "host_helper_authority_public_key": "11" * 32,
        "controller_endpoint": "https://agents.example.test:8443",
        "enrollment_endpoint": "https://enroll.example.test:8443",
        "controller_address": "192.168.1.231",
        "service_hostnames": [
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
        ],
    }
    assert len(response.content) < 64 * 1024
    assert "PRIVATE KEY" not in response.text


def test_bootstrap_has_one_current_response_even_with_an_obsolete_query(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )

    current = client.get("/agent/bootstrap")
    setup = client.get("/agent/bootstrap?setup_schema=1")

    assert current.status_code == setup.status_code == 200
    assert current.json() == setup.json()
    assert setup.json()["host_helper_authority_public_key"] == "11" * 32
    assert setup.content == canonical_message(setup.json())
    assert "PRIVATE KEY" not in setup.text


def test_bootstrap_requires_the_host_helper_authority(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    response = client.get("/agent/bootstrap")

    assert response.status_code == 503
    assert "host_helper_authority_public_key" not in response.json()
    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )
    accepted = client.get("/agent/bootstrap")
    assert accepted.status_code == 200
    assert accepted.json()["host_helper_authority_public_key"] == "11" * 32


@pytest.mark.usefixtures("damaged_json_rows")
def test_recipe_run_disposition_names_every_run_the_controller_does_not_want(
    agent_system,
) -> None:
    # Only a run the Controller can still stop is wanted. A run it never
    # owned, and a run it knows but failed or stopped, are both named unowned
    # so the agent retires its local claim instead of keeping it forever.
    client, services, _, clock = agent_system
    known_run_id = "70000000-0000-4000-8000-000000000071"
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=known_run_id,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=1,
                alias="known-exact",
                plan_digest="1" * 64,
                plan={"kind": "old"},
                state="failed",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.flush()
        session.add(
            RunNode(
                run_id=known_run_id,
                node_id=NODE_A,
                rank=0,
                role="main",
                state=RunState.FAILED,
                port=8000,
                reserved_memory_bytes=0,
                updated_at=clock.now,
            )
        )

    def disposition(run_id: str, headers: dict[str, str] | None = None):
        return client.get(
            f"/agent/recipe-runs/{run_id}/disposition",
            headers=agent_headers(NODE_A, "serial-a") if headers is None else headers,
        )

    def in_state(state: str, generation: int = 1):
        with services.sessions.begin() as session:
            run = session.get(RecipeRun, known_run_id)
            assert run is not None
            run.state = state
            run.run_generation = generation
        return disposition(known_run_id)

    never_owned = disposition("e85c4710-e437-4d12-8191-499596aa2a4c")
    assert never_owned.status_code == 204
    assert never_owned.headers["x-vonk-recipe-run-disposition"] == "unowned"
    for state in ("failed", "stopped"):
        not_wanted = in_state(state)
        assert not_wanted.status_code == 204
        assert not_wanted.headers["x-vonk-recipe-run-disposition"] == "unowned"
        assert "x-vonk-recipe-run-generation" not in not_wanted.headers
    for state in ("planned", "starting", "stopping"):
        wanted = in_state(state)
        assert "x-vonk-recipe-run-disposition" not in wanted.headers
        assert "x-vonk-recipe-run-generation" not in wanted.headers
    # A lost run's generation lets the agent report its process gone, which
    # releases the claims of a cancelled start.
    lost = in_state("lost", generation=2)
    assert "x-vonk-recipe-run-disposition" not in lost.headers
    assert lost.headers["x-vonk-recipe-run-generation"] == "2"
    running = in_state("running", generation=4)
    assert "x-vonk-recipe-run-disposition" not in running.headers
    assert running.headers["x-vonk-recipe-run-generation"] == "4"
    # A run wanted on A owns no files on B after re-enrollment/placement drift.
    foreign = disposition(known_run_id, agent_headers(NODE_B, "serial-b"))
    assert foreign.status_code == 204
    assert (
        foreign.headers.get("x-vonk-recipe-run-disposition")
        == RecipeRunDispositionValue.UNOWNED.value
    )
    assert "x-vonk-recipe-run-generation" not in foreign.headers
    assert disposition("E85C4710-not-a-run").status_code == 422
    assert disposition("e85c4710-e437-4d12-8191-499596aa2a4c", {}).status_code == 401


@pytest.mark.usefixtures("damaged_json_rows")
def test_recipe_run_observation_report_applies_process_state_per_run(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    run_id = "70000000-0000-4000-8000-000000000007"
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=run_id,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=2,
                alias="observed",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state="running",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            RunNode(
                run_id=run_id,
                node_id=NODE_A,
                rank=0,
                role="worker",
                state="running",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=clock.now - timedelta(seconds=1),
            )
        )

    def report(*runs: dict[str, object], observed_at: datetime = clock.now):
        return client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"observed_at": observed_at.isoformat(), "runs": list(runs)},
        )

    from vonk_agent_protocol.failure_evidence import FailureDiagnostics

    from .host_memory_guard_support import guard_diagnostics

    diagnostics = guard_diagnostics(clock.now.isoformat())
    run: dict[str, object] = {
        "run_id": run_id,
        "run_generation": 2,
        "process_running": False,
        "endpoint_ready": None,
        "failure_diagnostics": json.loads(canonical_message(diagnostics)),
    }
    stale_generation = report(run | {"run_generation": 1})
    # Superseded content is acknowledged without effects, so a durable sender
    # can advance to a fresh inspection instead of retrying a stale generation.
    assert stale_generation.status_code == 204
    # A report that crossed the Controller's own change (the run left this
    # Spark, or the rank changed after the report was taken) is expected:
    # it is dropped without error and never overwrites the rank.
    assert report(run | {"run_id": str(uuid.uuid4())}).status_code == 204
    assert report(run, observed_at=clock.now - timedelta(seconds=2)).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.observation_observed_at is None

    assert report(run | {"run_id": str(uuid.uuid4())}, run).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == "failed"
        assert node.observed_run_generation == 2
        assert node.observation_process_running is False
        assert node.observation_observed_at is not None
        # Wrong implementation: background inspection drops the safety cause,
        # leaving a hardware OOM indistinguishable from an ordinary exit.
        restored = FailureDiagnostics.model_validate_json(
            json.dumps(node.observation_failure_diagnostics)
        )
        assert restored == diagnostics
        assert (
            FailureDiagnostics.model_validate_json(
                json.dumps(node.last_exit_diagnostics)
            )
            == diagnostics
        )

    # New live evidence clears the old failure; it cannot poison a recovered run.
    recovered_at = clock.now + timedelta(seconds=1)
    assert (
        report(
            run | {"process_running": True, "failure_diagnostics": None},
            observed_at=recovered_at,
        ).status_code
        == 204
    )
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == RunState.RUNNING
        assert node.observation_failure_diagnostics is None
        assert node.observation_process_running is True
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == recovered_at

    # A delayed durable replay, even beyond the old five-minute acceptance
    # window, cannot undo the newer live observation or block the sender.
    clock.now += timedelta(minutes=10)
    assert report(run).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == RunState.RUNNING
        assert node.observation_failure_diagnostics is None
        assert node.observation_process_running is True
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == recovered_at


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_guard_stop_of_a_serving_co_tenant_is_not_the_runs_failure(
    agent_system,
) -> None:
    """The guard stops every container on the Spark, innocent ones included.

    Two runs share NODE_A. The run that was serving (seen alive at its current
    generation) is a co-tenant: its stop is kept as exit evidence only, so
    recovery relaunches it as it would any lost rank. The run never seen alive
    keeps the guard stop as its failure. Catches settling the co-tenant for good.
    """
    from .host_memory_guard_support import guard_diagnostics

    client, services, _, clock = agent_system
    serving, tried = (
        "70000000-0000-4000-8000-0000000000a1",
        "70000000-0000-4000-8000-0000000000a2",
    )
    with services.sessions.begin() as session:
        for run_id in (serving, tried):
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id="80000000-0000-4000-8000-00000000000a",
                    mapping_id="90000000-0000-4000-8000-00000000000a",
                    mapping_generation=1,
                    run_generation=1,
                    alias=f"co-tenant-{run_id[-1]}",
                    plan_digest="1" * 64,
                    plan={"schema_version": 1, "observation_schema_version": 2},
                    state="running",
                    route_state="withdrawn",
                    actor="admin",
                    created_at=clock.now,
                    updated_at=clock.now,
                )
            )
            session.add(
                RunNode(
                    run_id=run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="worker",
                    state="running",
                    port=8888 if run_id == serving else 8889,
                    reserved_memory_bytes=1,
                    updated_at=clock.now - timedelta(seconds=1),
                )
            )

    def report(run_id: str, *, running: bool, at: datetime):
        item: dict[str, object] = {
            "run_id": run_id,
            "run_generation": 1,
            "process_running": running,
            "endpoint_ready": None,
            "failure_diagnostics": None
            if running
            else json.loads(canonical_message(guard_diagnostics(at.isoformat()))),
        }
        return client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"observed_at": at.isoformat(), "runs": [item]},
        )

    assert report(serving, running=True, at=clock.now).status_code == 204
    stop = clock.now + timedelta(seconds=5)
    for run_id in (serving, tried):
        assert report(run_id, running=False, at=stop).status_code == 204
    # A replay of the same stop changes nothing.
    again = stop + timedelta(seconds=5)
    for run_id in (serving, tried):
        assert report(run_id, running=False, at=again).status_code == 204
    with services.sessions() as session:
        kept = {
            run_id: session.scalar(select(RunNode).where(RunNode.run_id == run_id))
            for run_id in (serving, tried)
        }
    assert all(
        node is not None and node.state != RunState.RUNNING for node in kept.values()
    )
    co_tenant, own = kept[serving], kept[tried]
    assert co_tenant is not None
    assert own is not None
    assert co_tenant.observation_failure_diagnostics is None
    assert co_tenant.last_exit_diagnostics is not None
    assert own.observation_failure_diagnostics is not None


def test_rust_agent_enrollment_shape_remains_controller_compatible(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    fixture = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/fixtures/enrollment-request.json"
        ).read_text()
    )
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    assert set(body) == set(fixture) == {"csr", "evidence", "grant_token"}
    assert set(body["evidence"]) == set(fixture["evidence"])
    response = client.post("/agent/enroll", json=body)

    assert response.status_code == 200
    assert response.json()["node_id"] == NODE_C


def test_enrollment_evidence_verdicts_answer_422_with_the_real_message(
    agent_system,
) -> None:
    # Wrong implementation: the verdicts were plain ValueErrors, so the 422
    # mapping turned them into a wait and pairing retried forever.
    client, services, _, _ = agent_system
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))
    for field, message in (
        ("node_id", "evidence node ID does not match CSR"),
        (
            "csr_public_key_fingerprint",
            "evidence CSR public-key fingerprint does not match CSR",
        ),
    ):
        broken = body | {"evidence": body["evidence"] | {field: NODE_A}}
        if field == "csr_public_key_fingerprint":
            broken["evidence"][field] = "0" * 64
        response = client.post("/agent/enroll", json=broken)
        assert response.status_code == 422
        assert message in response.text


def test_lost_enrollment_provider_write_heals_without_reissuing(
    agent_system, monkeypatch
) -> None:
    client, services, _, _ = agent_system
    calls = 0

    def fail_issue(
        _node_id: str,
        _csr: bytes,
        _now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        nonlocal calls
        services.enrollment._authority._begin(request)
        calls += 1
        raise RuntimeError("provider response lost")

    monkeypatch.setattr(services.enrollment._authority, "issue_node", fail_issue)
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    first = client.post("/agent/enroll", json=body)
    replay = client.post("/agent/enroll", json=body)

    # The retry observes the begun journal row and finishes it: no second issue.
    assert first.status_code == replay.status_code == 200
    assert calls == 1


def test_exact_enrollment_replay_returns_certificate_and_mismatch_is_denied(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    grant = services.enrollment.create(NODE_C, "administrator", 60)
    assert isinstance(grant, EnrollmentGrant)
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_C)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_C}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )
    public = (
        x509.load_pem_x509_csr(csr)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    body = {
        "grant_token": grant.token,
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_C,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }
    issued = client.post("/agent/enroll", json=body)
    pickup = client.post("/agent/enroll", json=body)
    mismatch = client.post(
        "/agent/enroll",
        json={**body, "evidence": {**body["evidence"], "boot_id": "different"}},
    )

    assert issued.status_code == pickup.status_code == 200
    assert issued.content == pickup.content
    assert pickup.content == canonical_message(pickup.json())
    assert pickup.json()["generation"] == 1
    assert "certificate_pem" in pickup.json()
    assert mismatch.status_code == 200
    assert mismatch.content == issued.content


def _csr_fingerprint(csr_pem: bytes) -> str:
    public_key = (
        x509.load_pem_x509_csr(csr_pem)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return hashlib.sha256(public_key).hexdigest()


def test_rotation_follower_heals_a_begun_journal_row_after_a_busy_reply(
    agent_system,
    monkeypatch,
) -> None:
    from vonk_agent_protocol.enrollment import IssuedCertificateResponse
    from vonk_control.step_ca import StepCAIssuancePending

    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        source = session.scalar(
            select(AgentCertificate).where(AgentCertificate.serial == "serial-a")
        )
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    request = _csr_for(NODE_A)
    claim = services.enrollment._claim_rotation(
        NODE_A, "101", request, _csr_fingerprint(request), clock.now
    )
    assert not isinstance(claim, IssuedCertificate)
    assert claim.provider_request is not None
    authority = services.enrollment._authority
    authority._begin(claim.provider_request)
    attempts = []
    observe = authority.observe_node

    def observed(*args, request, **kwargs):
        attempts.append(request)
        if len(attempts) == 1:
            raise StepCAIssuancePending("local CA journal is busy")
        return observe(*args, request=request, **kwargs)

    def forbidden_issue(*_args, **_kwargs):
        pytest.fail("observing the begun row finishes it; no second issue call")

    monkeypatch.setattr(authority, "observe_node", observed)
    monkeypatch.setattr(authority, "renew_node", forbidden_issue)

    response = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": request.decode()},
    )

    assert attempts == [claim.provider_request] * 2
    assert response.status_code == 200
    IssuedCertificateResponse.model_validate_json(response.content)
