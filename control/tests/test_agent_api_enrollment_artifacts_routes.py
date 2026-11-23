"""Agent API: enrollment artifacts routes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import delete

from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.enrollment import EnrollmentObservationReply
from vonk_control.agent_api.common import (
    EnrollmentRateLimiter,
)
from vonk_control.api.application import create_app
from vonk_control.auth import Actor
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.telemetry.samples import TelemetryRepository

from .agent_api_support import (
    NODE_A,
    NODE_B,
    NODE_C,
    PACKAGED_RUNTIME_IDENTITY,
    Jobs,
    _damage_storage_entry,
    agent_headers,
    asgi_post,
    assert_corrected_enrollment_succeeds,
    enrollment_grant,
    make_agent_system,
    parent,
    upgrade_payload,
    valid_enrollment_body,
)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        (b'{"grant_token":"', b'",]'),
        (b'{"grant_token":"', b'","invalid-utf8":"\xff"}'),
        (b"[" * 1500 + b'{"grant_token":"', b'"}' + b"]" * 1500),
    ],
    ids=("malformed-json", "invalid-utf8", "deep-nesting"),
)
def test_invalid_enrollment_json_preserves_identifiable_grant(
    agent_system,
    prefix: bytes,
    suffix: bytes,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        prefix + token.encode("ascii") + suffix,
    )

    assert status_code == 422
    assert_corrected_enrollment_succeeds(services, token)


def test_wrong_enrollment_content_type_preserves_identifiable_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        f'{{"grant_token":"{token}"}}'.encode("ascii"),
        content_type="text/plain",
    )

    assert status_code == 415
    assert_corrected_enrollment_succeeds(services, token)


def test_enrollment_evidence_has_a_fixed_bounded_schema(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/enroll",
        json={
            "grant_token": "a" * 43,
            "csr": "x",
            "evidence": {
                "node_id": NODE_A,
                "csr_public_key_fingerprint": "a" * 64,
                "host_key_fingerprint": "host",
                "hardware_fingerprint": "hardware",
                "agent_digest": "a" * 64,
                "boot_id": "boot",
                "unexpected": "x",
            },
        },
    )
    assert response.status_code == 422


def test_artifact_access_is_owned_content_addressed_and_range_bounded(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    response = client.get(
        f"/agent/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )
    # The Controller only authorizes: the edge reads the named file and
    # answers the range, so this response carries no bytes and no range.
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-vonk-file"] == (
        (services.artifact_root / digest).relative_to("/").as_posix()
    )
    assert response.headers["etag"] == f'"sha256:{digest}"'
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_B, "serial-b")
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/agent/artifacts/../secret", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/artifacts/{digest}",
            headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-99999999"},
        ).status_code
        == 416
    )


@pytest.mark.parametrize("damage", ["symlink", "directory", "fifo"])
def test_unsafe_artifact_is_removed_then_restored_by_producer(
    agent_system, tmp_path, damage
) -> None:
    client, services, _, clock = agent_system
    digest = "a" * 64
    (tmp_path / "outside").write_bytes(b"artifact")
    path = services.artifact_root / digest
    _damage_storage_entry(path, tmp_path / "outside", damage)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )

    assert not path.exists()
    assert not path.is_symlink()
    assert (tmp_path / "outside").read_bytes() == b"artifact"
    path.write_bytes(b"artifact")
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 200
    )


def test_artifact_is_named_to_the_edge_without_hashing_or_reading_the_file(
    agent_system,
) -> None:
    from .package_upgrade_fixtures import source_transport

    client, services, _, clock = agent_system
    payload = b"stored package bytes"
    # Same size, different content than the name says: authorized as stored,
    # which only holds if the route does not re-hash the object.
    digest = hashlib.sha256(b"a different payload").hexdigest()
    (services.artifact_root / digest).write_bytes(payload)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        {
            "schema_version": 1,
            "architecture": "linux-arm64",
            "package_bytes": len(payload),
            "package_sha256": digest,
            "package_signature": "a" * 128,
            "package_url": "https://install.vonkforge.ai/releases/test/vonk-forge-agent.deb",
            "package_version": "1.2.3",
            "target_binary_digest": "b" * 64,
            "target_build_digest": "sha256:" + "c" * 64,
            **source_transport(),
        },
    )

    response = client.get(
        f"/agent/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )

    assert response.status_code == 200
    assert response.headers["x-vonk-file"].endswith("/" + digest)


def test_retired_agent_update_tuf_routes_are_absent(agent_system) -> None:
    client, _, _, _ = agent_system
    headers = agent_headers(NODE_A, "serial-a")
    paths = client.get("/openapi.json").json()["paths"]

    assert not any(path.startswith("/agent/tuf/") for path in paths)
    assert (
        client.get("/agent/tuf/metadata/timestamp.json", headers=headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/tuf/targets/platform/releases/1.2.3/{'a' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )


def test_protected_agent_routes_gate_untrusted_invalid_bodies_before_parsing(
    agent_system,
) -> None:
    client, _, _, _ = agent_system
    for path in (
        "/agent/claim",
        "/agent/heartbeat",
        "/agent/result",
        "/agent/renew",
        "/agent/telemetry",
    ):
        assert (
            client.post(
                path, content=b"{not-json", headers={"content-type": "application/json"}
            ).status_code
            == 401
        )


def test_revoked_identity_is_gated_before_invalid_json_is_parsed(agent_system) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]
    assert (
        client.post(
            "/agent/result",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=b"{not-json",
        ).status_code
        == 401
    )


def test_job_wire_routes_publish_the_canonical_model_graph(agent_system) -> None:
    from vonk_agent_protocol.contracts import (
        AgentClaim,
        AgentDirective,
        AgentProgress,
        AgentResult,
    )
    from vonk_agent_protocol.wire_model import OperationProgress

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    for path, model in (("heartbeat", AgentProgress), ("result", AgentResult)):
        reference = paths[f"/agent/{path}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        document = components[reference.rsplit("/", 1)[-1]]
        assert document["title"] == model.__name__
        assert set(document["required"]) == set(model.model_json_schema()["required"])

    for path, model in (("claim", AgentClaim), ("heartbeat", AgentDirective)):
        reference = paths[f"/agent/{path}"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert components[reference.rsplit("/", 1)[-1]]["title"] == model.__name__
    assert "204" in paths["/agent/claim"]["post"]["responses"]

    # A compact serializer must not erase the outgoing progress structure.
    progress = OperationProgress.model_json_schema(mode="serialization")
    assert progress["additionalProperties"] is False
    assert {"phase", "completed_bytes", "checkpoint", "members"} <= set(
        progress["properties"]
    )


@pytest.mark.parametrize(
    "missing",
    [
        "protocol_version",
        "runtime_identity",
        "wait_seconds",
    ],
)
def test_claim_requires_the_current_agent_document(agent_system, missing: str) -> None:
    client, _services, _sessions, _clock = agent_system
    body = {
        "protocol_version": 4,
        "runtime_identity": dict(PACKAGED_RUNTIME_IDENTITY),
        "wait_seconds": 0,
    }
    del body[missing]
    # Use the raw request method: the convenience test client must not refill
    # deliberately missing fields and hide a production boundary regression.
    response = client.request(
        "POST",
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert response.status_code == 422


def test_enrollment_openapi_exposes_the_runtime_request_contract(agent_system) -> None:
    from vonk_agent_protocol.enrollment import EnrollmentSubmitRequest

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/agent/enroll"]["post"]
    request = operation["requestBody"]
    assert request["required"] is True
    assert request["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EnrollmentSubmitRequest"
    }
    expected = EnrollmentSubmitRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    nested = expected.pop("$defs")
    components = schema["components"]["schemas"]
    assert components["EnrollmentSubmitRequest"] == expected
    for name, document in nested.items():
        assert components[name] == document


@pytest.mark.parametrize("lose_response", [False, True])
def test_cli_enrollment_file_and_recovery_use_the_actual_authority(
    agent_system, tmp_path, capsys, lose_response
):
    from email.message import Message as EmailMessage
    from urllib.error import URLError

    from cluster_profiles import cli
    from cluster_profiles.control_client.transfers import ControlClient
    from vonk_control.operator_projection_api.wiring import (
        build_fleet_operator_services,
    )

    from .test_enrollment import csr, evidence

    _agent_api, services, codec, _clock = agent_system
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 0,
            fleet_services=build_fleet_operator_services(
                agent_services=services, upgrades=None
            ),
        )
    )
    identity = str(uuid.uuid4())
    actor = Actor("admin", "administrator")
    token_file = tmp_path / "cli-token"
    token_file.touch(mode=0o600)
    token_file.write_text(codec.issue(actor, ttl_seconds=3600, now=0))
    issued_secrets = []
    posts = []

    class OpenedResponse(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status = response.status_code
            self.headers = EmailMessage()
            for key, value in response.headers.items():
                self.headers[key] = value

        def __exit__(self, *args: object) -> None:
            self.close()

    def opener(request, *, timeout):
        response = api.request(
            request.get_method(),
            request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if request.get_method() == "POST":
            posts.append(request.full_url)
        if (
            request.full_url.endswith("/api/fleet/enroll")
            and response.status_code == 201
        ):
            issued_secrets.append(response.json()["grant"]["token"])
            if lose_response:
                raise URLError("response lost after acceptance")
        return OpenedResponse(response)

    client = ControlClient("https://forge.example.test", token_file, opener=opener)
    destination = tmp_path / "grant.json"
    code = cli.main(
        ("fleet", "enroll", "New Spark", "--output", str(destination), "--json"),
        control_client=client,
        request_id_factory=lambda: identity,
    )
    assert code == (2 if lose_response else 0), capsys.readouterr().out
    captured = capsys.readouterr()
    assert len(issued_secrets) == 1
    assert issued_secrets[0] not in captured.out + captured.err
    assert len(posts) == 1
    status_path = f"/api/fleet/enrollments/{identity}"
    assert client.request("GET", status_path)["state"] == "pending"
    assert api.get(status_path).status_code == 401
    for subject, role, expected in (
        ("viewer", "viewer", 403),
        ("other-admin", "administrator", 404),
    ):
        headers = {
            "Authorization": "Bearer "
            + codec.issue(Actor(subject, role), ttl_seconds=3600, now=0)
        }
        assert api.get(status_path, headers=headers).status_code == expected
        assert (
            api.post(status_path + "/revoke", headers=headers).status_code == expected
        )
    if lose_response:
        receipt = json.loads(destination.read_text())
        assert receipt["id"] == identity
        assert receipt["grant_status"]["state"] == "pending"
        assert (
            cli.main(
                ("fleet", "enrollment", "revoke", identity, "--yes", "--json"),
                control_client=client,
            )
            == 0
        )
        assert client.request("GET", status_path)["state"] == "revoked"
    else:
        grant = json.loads(destination.read_text())
        request = csr()
        assert services.enrollment is not None
        services.enrollment.submit(grant["token"], request, evidence(request))
        assert client.request("GET", status_path)["state"] == "consumed"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_reenrollment_refuses_an_unprivileged_actor_before_node_lookup(agent_system):
    api, _services, codec, _clock = agent_system
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("operator", "operator"), ttl_seconds=3600, now=0)
    }
    response = api.post(
        f"/api/fleet/{NODE_A}/re-enroll",
        headers=headers,
        json={"request_key": str(uuid.uuid4())},
    )
    assert response.status_code == 403


def test_enrollment_reader_miss_ends_and_fresh_same_node_is_admitted(
    agent_system, monkeypatch
):
    from vonk_control.step_ca import StepCAUnavailable

    client, services, _, _ = agent_system
    original = services.enrollment._authority.issue_node

    def unavailable(*args, **kwargs):
        raise StepCAUnavailable("reader unavailable")

    monkeypatch.setattr(services.enrollment._authority, "issue_node", unavailable)
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))
    response = client.post("/agent/enroll", json=body)
    assert response.status_code == 503
    EnrollmentObservationReply.model_validate_json(response.content)
    monkeypatch.setattr(services.enrollment._authority, "issue_node", original)
    fresh = json.loads(valid_enrollment_body(enrollment_grant(services)))
    repaired = client.post("/agent/enroll", json=fresh)
    assert repaired.status_code == 200
    assert repaired.json()["node_id"] == NODE_C


@pytest.mark.parametrize("failure", [None, "grace", "revoked", "removed", "wrong-key"])
def test_expired_renewal_endpoint_requires_enrolled_key_without_mtls(tmp_path, failure):
    from .test_enrollment import RecoveryAuthority, csr, enroll, expired_proof

    client, services, _codec, clock = make_agent_system(
        tmp_path, authority=RecoveryAuthority()
    )
    enrollment = services.enrollment
    assert enrollment is not None
    key = ed25519.Ed25519PrivateKey.generate()
    issued = enroll(enrollment, node_id=NODE_C, request=csr(NODE_C, key=key))
    clock.now = issued.not_after + timedelta(days=1)
    if failure == "grace":
        clock.now = issued.not_after + timedelta(days=30, seconds=1)
    if failure in {"revoked", "removed"}:
        with services.sessions.begin() as session:
            if failure == "removed":
                session.execute(
                    delete(AgentCertificate).where(AgentCertificate.node_id == NODE_C)
                )
                session.execute(delete(AgentNode).where(AgentNode.node_id == NODE_C))
            else:
                certificate = session.get(AgentCertificate, issued.serial)
                assert certificate is not None
                certificate.revoked_at = clock.now
    signer = ed25519.Ed25519PrivateKey.generate() if failure == "wrong-key" else key
    proof = expired_proof(signer, NODE_C, issued.serial, csr(NODE_C), clock.now)
    response = client.post(
        "/agent/renew/expired",
        content=proof.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    if failure is None:
        assert response.status_code == 200
        assert (
            client.post(
                "/agent/renew/expired",
                content=proof.model_dump_json(),
                headers={"content-type": "application/json"},
            ).json()
            == response.json()
        )
    else:
        assert response.status_code == 403
    # The proof-only exemption must never admit expired identity to work.
    assert client.post("/agent/claim", json={}).status_code == 401
    fresh_grant = enrollment.create("spk_" + "e" * 32, "admin", 60)
    assert isinstance(fresh_grant, EnrollmentGrant)
    assert fresh_grant.node_id == "spk_" + "e" * 32


def test_fleet_detail_reports_the_claimed_agent_build_and_updates_it(
    agent_system, capsys
):
    """An upgrade result is not proof of the binary that most recently claimed."""
    from cluster_profiles.cli_render.dispatch import render_payload
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.fleet_projection.service import FleetProjection

    client, services, _, clock = agent_system
    projection = FleetProjection(services.sessions, clock=clock)
    initial = next(node for node in projection.read().nodes if node.id == NODE_A)
    assert initial.agent_runtime is None
    first = AgentRuntimeIdentity.model_validate(PACKAGED_RUNTIME_IDENTITY)
    for identity in (
        first,
        first.model_copy(
            update={
                "semantic_version": "1.2.4",
                "build_digest": "sha256:" + "d" * 64,
                "binary_digest": "e" * 64,
            }
        ),
    ):
        response = client.post(
            "/agent/claim",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"runtime_identity": identity.model_dump(mode="json")},
        )
        assert response.status_code == 204, response.text
        node = next(node for node in projection.read().nodes if node.id == NODE_A)
        assert node.agent_runtime == identity
        render_payload(
            node.model_dump(mode="json", exclude_none=True), "fleet", action="detail"
        )
        output = capsys.readouterr().out
        assert identity.semantic_version in output
        assert identity.build_digest in output
        assert identity.binary_digest in output


def test_gpu_clock_failure_evidence_survives_http_storage_and_fleet_detail(
    agent_system, capsys
):
    """Catches dropping the whole clock when decoded JSON contains metric enums."""
    from cluster_profiles.cli_render.dispatch import render_payload
    from vonk_agent_protocol.gpu_clock import (
        GpuClockMetric,
        GpuClockObservation,
        GpuClockUnavailable,
        GpuClockUnavailableReason,
    )
    from vonk_agent_protocol.telemetry import TelemetryRequest, TelemetrySample
    from vonk_control.fleet_projection.service import FleetProjection

    client, services, _, clock = agent_system
    sample = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000001",
        observed_at=clock.now,
        memory_total_bytes=130_663_235_584,
        memory_available_bytes=126_065_086_464,
        disk_total_bytes=4_031_871_553_536,
        disk_free_bytes=1_290_377_957_376,
        gpu_utilization_percent=0.0,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        gpu_temperature_c=41,
        cpu_frequency_avg_mhz=3354,
        cpu_frequency_min_mhz=2808,
        cpu_frequency_max_mhz=3900,
    )

    # The production shape has fresh scalars and no clock field at all.
    def report(value: TelemetrySample) -> None:
        response = client.post(
            "/agent/telemetry",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=canonical_message(TelemetryRequest(samples=[value])),
        )
        assert response.status_code == 204, response.text

    report(sample)
    projection = FleetProjection(services.sessions, clock=clock)
    node = next(node for node in projection.read().nodes if node.id == NODE_A)
    assert node.telemetry is not None
    assert node.telemetry.sample is not None
    assert (
        "gpu_clock"
        not in node.model_dump(mode="json", exclude_none=True)["telemetry"]["sample"]
    )
    observation = GpuClockObservation(
        graphics_mhz=300,
        sm_mhz=300,
        unavailable=[
            GpuClockUnavailable(
                metric=GpuClockMetric.THROTTLE,
                reason=GpuClockUnavailableReason.NOT_SUPPORTED,
                detail="driver does not support throttle reasons",
            ),
            GpuClockUnavailable(
                metric=GpuClockMetric.MAX_SM,
                reason=GpuClockUnavailableReason.COMMAND_FAILED,
                detail="driver command failed",
            ),
        ],
    )
    clock.now += timedelta(seconds=2)
    report(
        sample.model_copy(update={"observed_at": clock.now, "gpu_clock": observation})
    )
    stored = TelemetryRepository(services.sessions, clock=clock).latest((NODE_A,))[
        NODE_A
    ]
    assert stored.gpu_clock == observation
    node = next(node for node in projection.read().nodes if node.id == NODE_A)
    assert node.telemetry is not None
    assert node.telemetry.sample is not None
    assert node.telemetry.sample.gpu_clock == observation
    render_payload(
        node.model_dump(mode="json", exclude_none=True), "fleet", action="detail"
    )
    output = capsys.readouterr().out
    assert "graphics 300 / SM 300 MHz" in output
    assert "driver does not support throttle reasons" in output
    assert "driver command failed" in output


def test_gpu_collection_reason_reaches_fleet_and_fresh_report_recovers(agent_system):
    # Catches ingress accepting the reason but omitting it from stored telemetry.
    from vonk_agent_protocol.telemetry import (
        GpuUnavailableReason,
        TelemetryRequest,
        TelemetrySample,
    )
    from vonk_control.fleet_projection.common import telemetry_point

    client, services, _, clock = agent_system
    failed = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000001",
        observed_at=clock.now,
        memory_total_bytes=128_000_000_000,
        memory_available_bytes=64_000_000_000,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        gpu_unavailable_reason=GpuUnavailableReason.COMMAND_FAILED,
    )
    repository = TelemetryRepository(services.sessions, clock=clock)
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=TelemetryRequest(samples=[failed]).model_dump(mode="json"),
    )
    assert response.status_code == 204
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is GpuUnavailableReason.COMMAND_FAILED
    clock.now += timedelta(seconds=1)
    recovered = failed.model_copy(
        update={
            "observed_at": clock.now,
            "gpu_unavailable_reason": None,
            "gpu_utilization_percent": 25.0,
            "gpu_temperature_c": 61,
        }
    )
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=TelemetryRequest(samples=[recovered]).model_dump(mode="json"),
    )
    assert response.status_code == 204
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is None
    assert point.gpu_utilization_percent == 25.0
    assert point.gpu_temperature_c == 61
    assert point.memory_total_bytes == 128_000_000_000
    assert point.gpu_memory_total_bytes is None


def test_enrollment_observation_does_not_block_unrelated_requests(
    agent_system, monkeypatch
):
    """Catches synchronous CA/SQL observation running on the async route's loop."""
    client, services, _, _ = agent_system
    original = services.enrollment._authority.issue_node
    entered = Event()
    release = Event()
    health_completed = Event()
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    def slow_issue(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(services.enrollment._authority, "issue_node", slow_issue)

    def observe():
        try:
            assert entered.wait(timeout=30)
            return health_completed.wait(timeout=30)
        finally:
            release.set()

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://testserver"
        ) as async_client:

            async def health_request():
                assert await asyncio.to_thread(entered.wait, 30)
                response = await async_client.get("/api/healthz")
                health_completed.set()
                return response

            with ThreadPoolExecutor(max_workers=1) as pool:
                observer = pool.submit(observe)
                pairing = asyncio.create_task(
                    async_client.post("/agent/enroll", json=body)
                )
                health = asyncio.create_task(health_request())
                try:
                    paired, responsive = await asyncio.wait_for(
                        asyncio.gather(pairing, health),
                        timeout=10,
                    )
                finally:
                    release.set()
                return paired, responsive, observer.result(timeout=30)

    paired, responsive, progressed = asyncio.run(exercise())
    assert progressed
    assert paired.status_code == responsive.status_code == 200
    replay = client.post("/agent/enroll", json=body)
    assert replay.status_code == 200
    assert replay.json() == paired.json()


@pytest.mark.parametrize(
    "damage", ["size", "symlink", "directory", "fifo", "directory-sync"]
)
def test_verified_upload_publication_repairs_or_preserves_exact_bytes(
    tmp_path, monkeypatch, damage
):
    # Wrong implementation: stored damage poisons a fresh verified publisher,
    # symlink bytes are accepted, or availability precedes durable directory IO.
    from vonk_control.agent_api.common import _commit_recipe_image_upload

    payload = b"verified archive"
    destination = tmp_path / hashlib.sha256(payload).hexdigest()
    temporary = tmp_path / "upload"
    temporary.write_bytes(payload)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    if damage == "size":
        destination.write_bytes(b"damaged")
    elif damage in {"symlink", "directory", "fifo"}:
        _damage_storage_entry(destination, outside, damage)
    real_sync = os.fsync
    synced = []

    def sync(descriptor):
        synced.append(os.fstat(descriptor).st_mode)
        if damage == "directory-sync" and len(synced) == 1:
            raise OSError("directory sync response lost")
        real_sync(descriptor)

    monkeypatch.setattr("vonk_control.agent_api.common.os.fsync", sync)
    try:
        _commit_recipe_image_upload(temporary, destination, expected_bytes=len(payload))
    except Exception:  # noqa: BLE001, S110 -- no unverified effect and fresh publication decide
        pass
    assert outside.read_bytes() == payload
    if damage != "directory-sync":
        import stat

        assert stat.S_ISREG(destination.lstat().st_mode)
        assert not destination.is_symlink()
        assert destination.read_bytes() == payload
    if not temporary.exists():
        temporary.write_bytes(payload)
    _commit_recipe_image_upload(temporary, destination, expected_bytes=len(payload))
    assert not destination.is_symlink()
    assert destination.read_bytes() == payload
    import stat

    assert any(stat.S_ISDIR(mode) for mode in synced)


def test_authorized_artifact_storage_unknown_ends_then_fresh_transfer_succeeds(
    agent_system, monkeypatch
):
    client, services, _, clock = agent_system
    payload = b"artifact"
    digest = hashlib.sha256(payload).hexdigest()
    path = services.artifact_root / digest
    path.write_bytes(payload)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.AGENT_UPGRADE,
        "a" * 64,
        upgrade_payload(digest, len(payload)),
    )
    real_stat = os.stat
    calls = []

    def stat_path(candidate, *args, **kwargs):
        if candidate == path:
            calls.append(candidate)
            raise OSError("storage temporarily unreadable")
        return real_stat(candidate, *args, **kwargs)

    monkeypatch.setattr("vonk_control.agent_api.common.os.stat", stat_path)
    route = f"/agent/artifacts/{digest}"
    unknown = client.get(route, headers=agent_headers(NODE_A, "serial-a"))
    assert 0 < int(unknown.headers["retry-after"]) <= 300
    assert "x-vonk-file" not in unknown.headers
    assert "etag" not in unknown.headers
    assert len(calls) == 4
    assert path.read_bytes() == payload
    calls.clear()

    def recovering_stat(candidate, *args, **kwargs):
        if candidate == path:
            calls.append(candidate)
            if len(calls) == 1:
                raise OSError("storage temporarily unreadable")
        return real_stat(candidate, *args, **kwargs)

    monkeypatch.setattr("vonk_control.agent_api.common.os.stat", recovering_stat)
    recovered = client.get(route, headers=agent_headers(NODE_A, "serial-a"))
    assert recovered.status_code == 200
    assert recovered.headers["etag"] == f'"sha256:{digest}"'
    assert len(calls) == 2


def test_expired_recovery_limit_cannot_starve_enrollment_and_clears(
    tmp_path, monkeypatch
):
    # Wrong implementation: recovery consumes enrollment's global budget or
    # a saturated recovery budget permanently prevents valid key-proof renewal.
    from vonk_control.agent_api import routes

    from .test_enrollment import RecoveryAuthority, csr, enroll, evidence, expired_proof

    _, services, codec, clock = make_agent_system(
        tmp_path, authority=RecoveryAuthority()
    )
    enrollment = services.enrollment
    assert enrollment is not None
    key = ed25519.Ed25519PrivateKey.generate()
    issued = enroll(enrollment, node_id=NODE_C, request=csr(NODE_C, key=key))
    clock.now = issued.not_after + timedelta(days=1)
    admitted_at = clock.now
    monotonic = lambda: (clock.now - admitted_at).total_seconds()
    recovery = EnrollmentRateLimiter(maximum=1, clock=monotonic)
    monkeypatch.setattr(routes, "EnrollmentRateLimiter", lambda **_: recovery)
    client = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            agent=services,
            enrollment_rate_limiter=EnrollmentRateLimiter(maximum=1, clock=monotonic),
        )
    )
    assert client.post("/agent/renew/expired", json={}).status_code == 422
    proof = expired_proof(key, NODE_C, issued.serial, csr(NODE_C), clock.now)
    limited = client.post(
        "/agent/renew/expired",
        content=proof.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert limited.status_code == 429
    operator_node = "spk_" + "d" * 32
    grant = enrollment.create(operator_node, "administrator", 60)
    assert isinstance(grant, EnrollmentGrant)
    request = csr(operator_node)
    assert (
        client.post(
            "/agent/enroll",
            json={
                "grant_token": grant.token,
                "csr": request.decode("ascii"),
                "evidence": evidence(request, node_id=operator_node),
            },
        ).status_code
        == 200
    )
    clock.now += timedelta(seconds=int(limited.headers["retry-after"]))
    recovered = client.post(
        "/agent/renew/expired",
        content=proof.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert recovered.status_code == 200
    assert recovered.json()["node_id"] == NODE_C


@pytest.mark.parametrize("damage", ["symlink", "directory", "fifo"])
@pytest.mark.parametrize("entry", ["upload", "publish"])
def test_unsafe_upload_entry_is_repaired(tmp_path, damage, entry):
    # Catches deterministic local damage poisoning fresh uploads/publication.
    from vonk_control.agent_api.common import (
        _commit_recipe_image_upload,
        _prepare_recipe_image_upload,
    )

    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    destination = tmp_path / "image"
    damaged = tmp_path / (".identity.upload" if entry == "upload" else ".image.publish")
    _damage_storage_entry(damaged, outside, damage)
    descriptor, temporary = _prepare_recipe_image_upload(tmp_path, "identity")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"verified archive")
    _commit_recipe_image_upload(temporary, destination, expected_bytes=16)
    assert destination.read_bytes() == b"verified archive"
    assert outside.read_bytes() == b"untouched"
    if entry == "publish":
        assert damaged.is_file()
        assert not damaged.is_symlink()
    else:
        assert not damaged.exists()
    descriptor, temporary = _prepare_recipe_image_upload(tmp_path, "identity")
    os.close(descriptor)
    assert temporary.is_file()
    assert not temporary.is_symlink()
