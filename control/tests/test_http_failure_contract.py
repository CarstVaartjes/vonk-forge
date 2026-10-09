"""Provider/consumer behavior at the common HTTP and live renewal boundaries."""

import pytest
from vonk_agent_protocol.http_failure import (
    HttpFailureResponse,
    HttpRefusal,
    HttpTransient,
)
from vonk_control.models import AgentCertificate

from cluster_profiles.control_client import ControlClient
from cluster_profiles.control_client.common import (
    observation_delay,
    observation_unknown,
)
from cluster_profiles.control_client.errors import ControlHTTPError

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as agent_system  # noqa: PLC0414
from .test_enrollment import csr


@pytest.mark.parametrize("provider_state", ["CA restarting", "disk full"])
def test_authenticated_renewal_heals_then_accepts_fresh_rotation(
    agent_system, monkeypatch, provider_state
):
    """Catches persistent denial/lost CSR ownership after a provider outage."""
    api, services, _, _ = agent_system
    with services.sessions.begin() as session:
        source = session.get(AgentCertificate, "serial-a")
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    authority = services.enrollment._authority
    original = authority.renew_node

    def unavailable(*args, **kwargs):
        raise OSError(provider_state)

    monkeypatch.setattr(authority, "renew_node", unavailable)
    headers = agent_headers(NODE_A, "101")
    request = {"node_id": NODE_A, "csr": csr(NODE_A).decode("ascii")}
    failed = api.post("/agent/renew", headers=headers, json=request)
    envelope = HttpFailureResponse.model_validate_json(failed.headers["x-vonk-outcome"])
    assert isinstance(envelope.failure, HttpTransient)
    assert envelope.failure.retry_after >= 0
    assert envelope.failure.resolution_window > 0
    monkeypatch.setattr(authority, "renew_node", original)
    healed = api.post("/agent/renew", headers=headers, json=request)
    assert healed.status_code == 200
    issued = healed.json()
    headers = agent_headers(NODE_A, issued["serial"])
    headers["x-vonk-agent-fingerprint"] = issued["fingerprint"]
    activated = api.post(
        "/agent/renew/activate",
        headers=headers,
        json={"node_id": NODE_A, "generation": issued["generation"]},
    )
    assert activated.status_code == 204
    authority._serial = int(issued["serial"])
    fresh = api.post(
        "/agent/renew",
        headers=headers,
        json={"node_id": NODE_A, "csr": csr(NODE_A).decode("ascii")},
    )
    assert fresh.status_code == 200


def test_security_edge_is_not_retried_but_fresh_authority_is_admitted(agent_system):
    """Catches a bare HTTP denial and a denial that latches future requests."""
    api, services, _, _ = agent_system
    with services.sessions.begin() as session:
        source = session.get(AgentCertificate, "serial-a")
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    request = {"node_id": NODE_A, "csr": csr(NODE_A).decode("ascii")}
    denied = api.post("/agent/renew", json=request)
    envelope = HttpFailureResponse.model_validate_json(denied.headers["x-vonk-outcome"])
    assert isinstance(envelope.failure, HttpRefusal)
    family, retry = ControlClient._http_outcome(denied.headers)
    consumer = ControlHTTPError(
        denied.status_code, "denied", retry, failure_family=family
    )
    assert not observation_unknown(consumer)
    assert (
        api.post(
            "/agent/renew", headers=agent_headers(NODE_A, "101"), json=request
        ).status_code
        == 200
    )


def test_unreadable_peer_answer_is_bounded_unknown_and_server_delay_is_minimum():
    """Catches untyped 403 becoming security refusal or jitter shortening RetryInfo."""
    family, retry = ControlClient._http_outcome({"x-vonk-outcome": "damaged"})
    unknown = ControlHTTPError(403, "unknown", retry, failure_family=family)
    assert observation_unknown(unknown)
    unknown.retry_after_seconds = 5
    for attempt in range(8):
        assert 5 <= observation_delay(unknown, attempt, 20) <= 8.2
    assert observation_delay(unknown, 0, 2) == 2


def test_monitor_health_survives_storage_and_projection_then_clears(agent_system):
    """Catches accepting monitor health while dropping it before metrics export."""
    import json

    from vonk_agent_protocol.telemetry import TelemetryRequest
    from vonk_control.fleet_projection.common import telemetry_point
    from vonk_control.metrics import MetricsRegistry
    from vonk_control.telemetry import TelemetryRepository

    from .test_agent_api import telemetry_payload
    from .test_metrics import _fleet_snapshot

    api, services, _, clock = agent_system
    request = TelemetryRequest.model_validate_json(json.dumps(telemetry_payload(clock)))
    request = request.model_copy(
        update={
            "samples": [
                request.samples[0].model_copy(
                    update={
                        "renewal_failed": True,
                        "credential_remaining_fraction": 0.2,
                    }
                )
            ]
        }
    )
    assert (
        api.post(
            "/agent/telemetry",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=request.model_dump_json(),
        ).status_code
        == 204
    )
    repository = TelemetryRepository(services.sessions, clock=clock)
    observed = repository.latest([NODE_A])[NODE_A]
    snapshot = _fleet_snapshot()
    snapshot.nodes[0].id = NODE_A
    assert snapshot.nodes[0].telemetry is not None
    snapshot.nodes[0].telemetry.sample = telemetry_point(observed)
    metrics = MetricsRegistry()
    metrics.update_fleet(snapshot)
    assert f'vonk_agent_renewal_failed{{node_id="{NODE_A}"}} 1' in metrics.render()
    assert (
        f'vonk_agent_credential_low_lifetime{{node_id="{NODE_A}"}} 1'
        in metrics.render()
    )
    snapshot.nodes[0].telemetry.sample.renewal_failed = False
    snapshot.nodes[0].telemetry.sample.credential_remaining_fraction = 0.5
    metrics.update_fleet(snapshot)
    assert f'vonk_agent_renewal_failed{{node_id="{NODE_A}"}} 0' in metrics.render()
    assert (
        f'vonk_agent_credential_low_lifetime{{node_id="{NODE_A}"}} 0'
        in metrics.render()
    )


def test_only_explicit_security_producers_can_emit_a_refusal(tmp_path):
    """Catches a new storage/unknown HTTP status being promoted into authority."""
    import ast
    from pathlib import Path

    from fastapi.testclient import TestClient
    from starlette.responses import Response
    from vonk_control.contract_graph import schema_application

    root = Path(__file__).parents[1] / "src/vonk_control"
    untyped_denials = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            status = next(
                (item.value for item in node.keywords if item.arg == "status_code"),
                None,
            )
            if (
                node.func.id == "HTTPException"
                and isinstance(status, ast.Constant)
                and status.value in (401, 403)
            ):
                untyped_denials.append(f"{path.name}:{node.lineno}")
    assert not untyped_denials
    app = schema_application()

    @app.get("/api/provider-state")
    def unknown_provider():
        return Response(status_code=403)

    response = TestClient(app).get("/api/provider-state")
    envelope = HttpFailureResponse.model_validate_json(
        response.headers["x-vonk-outcome"]
    )
    assert isinstance(envelope.failure, HttpTransient)
    assert envelope.failure.retry_after >= 0


def test_enrollment_provider_state_heals_without_consuming_fresh_authority(
    agent_system, monkeypatch
):
    """A CA outage cannot latch an enrollment or consume the next grant."""
    import json

    from .test_agent_api import enrollment_grant, valid_enrollment_body

    api, services, _, _ = agent_system
    authority = services.enrollment._authority
    issue = authority.issue_node

    def unavailable(*args, **kwargs):
        raise OSError("CA restarting")

    body = json.loads(valid_enrollment_body(enrollment_grant(services)))
    monkeypatch.setattr(authority, "issue_node", unavailable)
    ended = api.post("/agent/enroll", json=body)
    answer = HttpFailureResponse.model_validate_json(ended.headers["x-vonk-outcome"])
    assert isinstance(answer.failure, HttpTransient)
    assert answer.failure.retry_after >= 0
    monkeypatch.setattr(authority, "issue_node", issue)
    healed = api.post("/agent/enroll", json=body)
    assert healed.status_code == 200, healed.text
    from vonk_control.enrollment_contract import EnrollmentGrant

    from .test_agent_api import NODE_C

    authority._serial = int(healed.json()["serial"])
    grant = services.enrollment.create_reenrollment(
        NODE_C, "administrator", 60, request_key="33333333-3333-4333-8333-333333333333"
    )
    assert isinstance(grant, EnrollmentGrant)
    fresh = json.loads(valid_enrollment_body(grant.token))
    assert api.post("/agent/enroll", json=fresh).status_code == 200


def test_profile_load_provider_state_heals_then_accepts_fresh_request(monkeypatch):
    """A starting Controller does not leave a request/selection admission gate."""
    from vonk_control.fleet_profiles import FleetProfileService

    from .test_fleet_profile_api import _client, _headers

    api, codec = _client(with_idle_spark=True)
    headers = _headers(codec, "administrator")
    assert (
        api.put(
            "/api/profile/1",
            headers=headers,
            json={"name": "Idle", "expected_revision": 0},
        ).status_code
        == 200
    )
    load = FleetProfileService.load

    def unavailable(*args, **kwargs):
        raise OSError("Controller starting")

    monkeypatch.setattr(FleetProfileService, "load", unavailable)
    request = {"request_key": "22222222-2222-4222-8222-222222222222"}
    ended = api.post("/api/profile/1/load", headers=headers, json=request)
    answer = HttpFailureResponse.model_validate_json(ended.headers["x-vonk-outcome"])
    assert isinstance(answer.failure, HttpTransient)
    assert answer.failure.retry_after >= 0
    monkeypatch.setattr(FleetProfileService, "load", load)
    assert (
        api.post("/api/profile/1/load", headers=headers, json=request).status_code
        == 202
    )
    assert (
        api.post(
            "/api/profile/1/load",
            headers=headers,
            json={"request_key": "33333333-3333-4333-8333-333333333333"},
        ).status_code
        == 202
    )


def test_distribution_provider_state_heals_then_serves_fresh_assignment(
    agent_system, monkeypatch
):
    """Unavailable object custody cannot become an assignment security denial."""
    from vonk_agent_protocol import DistributionCode
    from vonk_control.distribution import (
        DistributionService,
        DistributionUnknown,
        MemoryObjectSource,
    )

    from .test_distribution import _assignment

    api, services, _, clock = agent_system
    source = MemoryObjectSource()
    model = source.put(b"model payload")
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(NODE_A, model, config, archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    observe = service.authorize

    def unavailable(*args, **kwargs):
        raise DistributionUnknown(DistributionCode.OBJECT_INVALID, "journal damaged")

    monkeypatch.setattr(service, "authorize", unavailable)
    route = f"/agent/distribution/manifests/{assignment.plan_digest}"
    headers = agent_headers(NODE_A, "serial-a")
    ended = api.get(route, headers=headers)
    answer = HttpFailureResponse.model_validate_json(ended.headers["x-vonk-outcome"])
    assert isinstance(answer.failure, HttpTransient)
    assert answer.failure.retry_after >= 0
    monkeypatch.setattr(service, "authorize", observe)
    assert api.get(route, headers=headers).status_code == 200
    assert api.get(route, headers=headers).status_code == 200


def test_stop_provider_state_heals_without_holding_fresh_claims(
    agent_system, monkeypatch
):
    """Lost journal observation must not consume a stop fence or its successor."""
    from vonk_agent_protocol.contracts import AgentOperation

    from .test_agent_api import STOP_PAYLOAD, parent

    api, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        AgentOperation.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = services.operations.claim

    def unavailable(*args, **kwargs):
        raise OSError("journal damaged")

    monkeypatch.setattr(services.operations, "claim", unavailable)
    headers = agent_headers(NODE_A, "serial-a")
    ended = api.post("/agent/claim", headers=headers)
    answer = HttpFailureResponse.model_validate_json(ended.headers["x-vonk-outcome"])
    assert isinstance(answer.failure, HttpTransient)
    assert answer.failure.retry_after >= 0
    monkeypatch.setattr(services.operations, "claim", claim)
    first = api.post("/agent/claim", headers=headers)
    assert first.status_code == 200
    from vonk_agent_protocol import (
        AgentClaim,
        AgentResult,
        AgentResultState,
        OutcomeKind,
    )
    from vonk_agent_protocol.outcome import OutcomeDone
    from vonk_agent_protocol.recipe_operations import RecipeStopResult

    accepted = AgentClaim.model_validate_json(first.content)
    completion = AgentResult(
        fence=accepted.fence,
        state=AgentResultState.SUCCEEDED,
        result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
    )
    assert (
        api.post(
            "/agent/result", headers=headers, json=completion.model_dump(mode="json")
        ).status_code
        == 204
    )
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        AgentOperation.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    assert api.post("/agent/claim", headers=headers).status_code == 200
