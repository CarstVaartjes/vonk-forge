from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from threading import Event

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from hypothesis import example, given
from hypothesis import strategies as st
from sqlalchemy import delete, event, select
from vonk_agent_protocol import (
    ControllerErrorCode,
    LifecycleState,
    RouteState,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    AgentClientDecision,
)
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.enrollment import EnrollmentObservationReply
from vonk_agent_protocol.error_response import BoundedErrorResponse
from vonk_agent_protocol.http_failure import HttpTransient, TransientReason
from vonk_agent_protocol.inventory import (
    HostSettings,
    InventoryRequest,
)
from vonk_agent_protocol.recipe_observations import RecipeRunObservationsWire
from vonk_control.agent_api import (
    EnrollmentRateLimiter,
    _scan_enrollment_grants,
)
from vonk_control.agent_api.common import _ENROLLMENT_TOKEN
from vonk_control.api import create_app
from vonk_control.auth import Actor
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    RecipeRun,
    RunNode,
)
from vonk_control.telemetry.samples import TelemetryRepository

from . import agent_api_support as _support
from .agent_api_support import (
    NODE_A,
    NODE_C,
    PACKAGED_RUNTIME_IDENTITY,
    STOP_PAYLOAD,
    Jobs,
    _csr_for,
    _damage_storage_entry,
    agent_headers,
    enrollment_grant,
    make_agent_system,
    parent,
    upgrade_payload,
    valid_enrollment_body,
)
from .agent_jobs_support import canonical_start_payload

agent_system = _support.agent_system


def test_job_wire_routes_publish_the_canonical_model_graph(agent_system) -> None:
    from vonk_agent_protocol import (
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
    (
        "protocol_version",
        "runtime_identity",
        "wait_seconds",
    ),
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

    from vonk_control.operator_projection_api import build_fleet_operator_services

    from cluster_profiles import cli
    from cluster_profiles.control_client import ControlClient

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
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.fleet_projection import FleetProjection

    from cluster_profiles.cli_render import render_payload

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
    from vonk_agent_protocol.gpu_clock import (
        GpuClockMetric,
        GpuClockObservation,
        GpuClockUnavailable,
        GpuClockUnavailableReason,
    )
    from vonk_agent_protocol.telemetry import TelemetryRequest, TelemetrySample
    from vonk_control.fleet_projection import FleetProjection

    from cluster_profiles.cli_render import render_payload

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
    assert node.telemetry is not None and node.telemetry.sample is not None
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
    assert node.telemetry is not None and node.telemetry.sample is not None
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
    from vonk_control.fleet_projection import telemetry_point

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
        assert damaged.is_file() and not damaged.is_symlink()
    else:
        assert not damaged.exists()
    descriptor, temporary = _prepare_recipe_image_upload(tmp_path, "identity")
    os.close(descriptor)
    assert temporary.is_file() and not temporary.is_symlink()


@pytest.mark.parametrize(
    "failure", ["denied-open", "denied-mkdir", "transient", "outside"]
)
def test_upload_storage_failure_preserves_agent_authority(
    tmp_path, monkeypatch, caplog, failure
):
    # Catches NAS storage faults incorrectly sending an agent to re-enrollment.
    from fastapi import HTTPException
    from vonk_control.agent_api.common import _prepare_recipe_image_upload

    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    root.mkdir()
    if failure == "outside":
        outside.mkdir()
        (outside / ".identity.upload").write_bytes(b"untouched")
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    else:

        def unavailable(*args, **kwargs):
            raise (
                PermissionError("denied")
                if failure.startswith("denied")
                else OSError("I/O unavailable")
            )

        if failure == "denied-mkdir":
            monkeypatch.setattr(Path, "mkdir", unavailable)
        else:
            monkeypatch.setattr("vonk_control.agent_api.common.os.open", unavailable)
    with pytest.raises(HTTPException) as refused:
        _prepare_recipe_image_upload(root, "identity")
    assert refused.value.status_code == 503
    assert refused.value.headers is not None
    assert refused.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(refused.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    if failure != "transient":
        assert "operator attention" in caplog.text
    if failure == "outside":
        assert (outside / ".identity.upload").read_bytes() == b"untouched"
        root.unlink()
    monkeypatch.undo()
    descriptor, temporary = _prepare_recipe_image_upload(root, "identity")
    os.close(descriptor)
    assert temporary.is_file() and not temporary.is_symlink()


@pytest.mark.parametrize("damage", ["symlink", "fifo"])
def test_upload_storage_changed_entry_retries_then_repairs(
    tmp_path, monkeypatch, damage
):
    # Catches a swap after cleanup producing a terminal 409 or blocking on a FIFO.
    from fastapi import HTTPException
    from vonk_control.agent_api import common

    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    original_remove = common._remove_unsafe_storage_entry

    def swap_after_removal(entry):
        original_remove(entry)
        _damage_storage_entry(entry, outside, damage)

    with monkeypatch.context() as patch:
        patch.setattr(common, "_remove_unsafe_storage_entry", swap_after_removal)
        with pytest.raises(HTTPException) as unavailable:
            common._prepare_recipe_image_upload(tmp_path, "identity")
    assert unavailable.value.status_code == 503
    assert unavailable.value.headers is not None
    assert unavailable.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(unavailable.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    descriptor, temporary = common._prepare_recipe_image_upload(tmp_path, "identity")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"verified archive")
    assert temporary.read_bytes() == b"verified archive"
    assert not temporary.is_symlink()
    assert outside.read_bytes() == b"untouched"


def test_publication_permission_failure_retries_then_succeeds(
    tmp_path, monkeypatch, caplog
):
    # Catches publication permission failures incorrectly revoking agent authority.
    from fastapi import HTTPException
    from vonk_control.agent_api import common

    temporary = tmp_path / ".identity.upload"
    temporary.write_bytes(b"verified archive")
    destination = tmp_path / "image"

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(common.os, "replace", denied)
        with pytest.raises(HTTPException) as unavailable:
            common._commit_recipe_image_upload(
                temporary, destination, expected_bytes=16
            )
    assert unavailable.value.status_code == 503
    assert unavailable.value.headers is not None
    assert unavailable.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(unavailable.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    assert "operator attention" in caplog.text
    common._commit_recipe_image_upload(temporary, destination, expected_bytes=16)
    assert destination.read_bytes() == b"verified archive"


@pytest.mark.parametrize("failure", ["stat", "repair"])
def test_artifact_permission_failure_retries_after_storage_recovers(
    agent_system, monkeypatch, caplog, failure
):
    # Catches storage stat/repair failures triggering re-enrollment instead of recovery.
    from vonk_control.agent_api import common

    client, services, _, clock = agent_system
    digest = "a" * 64
    path = services.artifact_root / digest
    if failure == "repair":
        path.mkdir()
    else:
        path.write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    original = (
        common.os.stat if failure == "stat" else common._remove_unsafe_storage_entry
    )

    def denied(entry, *args, **kwargs):
        if entry == path:
            raise PermissionError("denied")
        return original(entry, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "stat":
            patch.setattr(common.os, "stat", denied)
        else:
            patch.setattr(common, "_remove_unsafe_storage_entry", denied)
        unavailable = client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        )
    assert unavailable.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert unavailable.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(unavailable.text).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    assert "operator attention" in caplog.text
    if failure == "repair":
        path.rmdir()
        path.write_bytes(b"artifact")
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 200
    )


def test_display_memory_agent_vectors_reobserve_through_http_store_and_cli(
    agent_system, capsys
):
    """Catches lost JSON enum evidence, sticky advice, or inventing unknown bytes."""
    from vonk_agent_protocol import ProjectionCode
    from vonk_control.fleet_projection import FleetProjection

    from cluster_profiles.cli_render import render_payload

    client, services, _, clock = agent_system
    vectors = json.loads(
        files("vonk_agent_protocol").joinpath("vectors/display-memory.json").read_text()
    )
    projection = FleetProjection(services.sessions, clock=clock)
    baseline = InventoryRequest.model_validate_json(
        files("vonk_agent_protocol")
        .joinpath("vectors/inventory-wifi-link-v1.json")
        .read_bytes()
    )
    # Known default -> unavailable read -> firmware ready -> minimum after reboot.
    for index in (0, 2, 3, 1):
        host = HostSettings.model_validate_json(canonical_message(vectors[index]))
        clock.now += timedelta(seconds=2)
        payload = baseline.model_copy(
            update={"observed_at": clock.now, "host_settings": host}
        )
        response = client.post(
            "/agent/inventory",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=canonical_message(payload),
        )
        assert response.status_code == 204, response.text
        node = next(node for node in projection.read().nodes if node.id == NODE_A)
        assert node.inventory is not None
        assert node.inventory.host_settings == host
        advice = [
            warning
            for warning in node.warnings
            if warning.code == ProjectionCode.HOST_DISPLAY_MEMORY
        ]
        assert bool(advice) == (index in (0, 3))
        render_payload(
            node.model_dump(mode="json", exclude_none=True), "fleet", action="detail"
        )
        captured = capsys.readouterr()
        output = captured.out + captured.err
        observation = host.display_memory
        assert observation is not None
        if observation.carveout_bytes is None:
            assert "Display memory: reserved unknown" in output
        if observation.soc_firmware_version is not None:
            assert f"0x{observation.soc_firmware_version:08x}" in output
        assert ("fwupdmgr update" in output) == (index == 0)
        assert ("Display Reserved Memory to 0.5 GB" in output) == bool(advice)
        assert ("0.5 GB (fixed)" in output) == bool(advice)


def test_claim_stale_recipe_run_consumer_waits_then_admits(
    tmp_path,
    postgres_engine,
) -> None:
    """A lost run fence in the plain claim transaction must not become HTTP 500."""
    from .recipe_routes_support import setup

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    routes, _, _, run_id = setup(tmp_path / "routes", engine=postgres_engine)

    # The spent-start result consumer shares _claim_once's plain transaction.
    # Like consume_agent_result's child-only write, it must fence the run too.
    def consume(session, _operation, _attempt, _message):
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.version += 1

    services.operations.set_result_consumer(consume)
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_START,
        "a" * 64,
        payload,
    )
    headers = agent_headers(NODE_A, "serial-a")
    assert client.post("/agent/claim", headers=headers).status_code == 200
    clock.now += timedelta(minutes=31)

    conflicts = []

    def concurrent_writer(session, _context, _instances):
        if conflicts or not any(isinstance(row, RecipeRun) for row in session.dirty):
            return
        conflicts.append(run_id)
        with routes.sessions.begin() as winner:
            run = winner.get(RecipeRun, run_id)
            assert run is not None
            run.route_error = "concurrent accepted decision"

    event.listen(services.sessions, "before_flush", concurrent_writer)
    try:
        response = client.post("/agent/claim", headers=headers)
    finally:
        event.remove(services.sessions, "before_flush", concurrent_writer)
    assert conflicts == [run_id]
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"] == ControllerErrorCode.UNAVAILABLE
    assert int(response.headers["retry-after"]) > 0
    problem = BoundedErrorResponse.model_validate_json(response.content)
    assert problem.context is not None
    assert problem.context.decision == AgentClientDecision.RETRY
    assert problem.context.retryable is True
    with services.sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == LifecycleState.RUNNING
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    assert client.post("/agent/claim", headers=headers).status_code == 200


def test_recipe_observation_contention_waits_then_redelivery_commits(
    tmp_path,
    postgres_engine,
) -> None:
    """Continued contention must roll back observations and acknowledge redelivery."""
    from .recipe_routes_support import setup

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    routes, _, _, run_id = setup(tmp_path / "routes", engine=postgres_engine)
    with services.sessions.begin() as session:
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id).limit(1))
        assert rank is not None
        rank.node_id = NODE_A
        rank.updated_at = clock.now - timedelta(seconds=1)
        rank.observed_run_generation = None
        rank.observation_process_running = None
        rank.observation_observed_at = None
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.route_state = RouteState.WITHDRAWN
        run.next_action_at = clock.now
        generation = run.run_generation
    body = RecipeRunObservationsWire.model_validate(
        {
            "observed_at": clock.now,
            "runs": [
                {
                    "run_id": run_id,
                    "run_generation": generation,
                    "process_running": True,
                    "endpoint_ready": True,
                }
            ],
        }
    ).model_dump(mode="json")
    conflicts = []

    def concurrent_writer(session, _context, _instances):
        if not any(isinstance(row, RecipeRun) for row in session.dirty):
            return
        conflicts.append(run_id)
        with routes.sessions.begin() as winner:
            run = winner.get(RecipeRun, run_id)
            assert run is not None
            run.route_error = f"concurrent decision {len(conflicts)}"

    event.listen(services.sessions, "before_flush", concurrent_writer)
    try:
        response = client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body,
        )
    finally:
        event.remove(services.sessions, "before_flush", concurrent_writer)
    assert len(conflicts) == 2  # transition re-read once, then yielded Stale.
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"] == ControllerErrorCode.UNAVAILABLE
    assert int(response.headers["retry-after"]) > 0
    with services.sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.node_id == NODE_A))
        assert rank is not None and rank.observation_observed_at is None
    delivered = client.post(
        "/agent/recipe-runs/observations",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert delivered.status_code == 204
    with services.sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.node_id == NODE_A))
        assert rank is not None and rank.observation_process_running is True
        assert rank.observed_run_generation == generation


@pytest.mark.parametrize("action", ["succeed", "fail"])
def test_result_scope_changed_yields_without_outer_replay(
    agent_system, monkeypatch, action
):
    """SCOPE_CHANGED already exhausted transition's fresh decision; do not replay it."""
    _, services, _, _ = agent_system
    calls = []
    refused = UnknownOutcomeError("run changed", reason=WaitReason.SCOPE_CHANGED)

    def finish(*args, **kwargs):
        calls.append(args)
        raise refused

    monkeypatch.setattr(services.operations, "_finish", finish)
    with pytest.raises(UnknownOutcomeError) as caught:
        if action == "succeed":
            services.operations.succeed("unused-fence", {})
        else:
            services.operations.fail("unused-fence", "invalid result")
    assert caught.value is refused
    assert len(calls) == 1


def _renewable_source(services) -> None:
    with services.sessions.begin() as session:
        source = session.scalar(
            select(AgentCertificate).where(AgentCertificate.serial == "serial-a")
        )
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"


def _renewal_body() -> dict[str, str]:
    return {"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()}


def test_internal_error_during_renewal_is_a_wait_not_a_fatal_refusal(
    agent_system, monkeypatch
) -> None:
    # Wrong implementation: any ValueError answered 403, a security verdict the
    # Spark backs off on for 30-600 s, for a Controller-side bug.
    client, services, _, _ = agent_system
    _renewable_source(services)

    def broken(*_args, **_kwargs):
        raise ValueError("internal decode failure")

    monkeypatch.setattr(services.enrollment, "renew", broken)
    response = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert response.status_code == 503
    assert 0 < int(response.headers["Retry-After"]) <= 300


def test_real_denial_during_renewal_stays_a_403(agent_system, monkeypatch) -> None:
    from vonk_control.enrollment.types import EnrollmentDenied

    client, services, _, _ = agent_system
    _renewable_source(services)

    def denied(*_args, **_kwargs):
        raise EnrollmentDenied("certificate is revoked")

    monkeypatch.setattr(services.enrollment, "renew", denied)
    response = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert response.status_code == 403


def test_renewal_heals_after_one_internal_error(agent_system, monkeypatch) -> None:
    client, services, _, _ = agent_system
    _renewable_source(services)
    real = services.enrollment.renew
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("transient internal failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(services.enrollment, "renew", flaky)
    first = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert first.status_code == 503
    again = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert len(calls) == 2
    assert again.status_code == 200


def test_expired_renewal_internal_error_waits_and_heals(tmp_path, monkeypatch) -> None:
    from .test_enrollment import RecoveryAuthority, csr, enroll, expired_proof

    client, services, _codec, clock = make_agent_system(
        tmp_path, authority=RecoveryAuthority()
    )
    enrollment = services.enrollment
    assert enrollment is not None
    key = ed25519.Ed25519PrivateKey.generate()
    issued = enroll(enrollment, node_id=NODE_C, request=csr(NODE_C, key=key))
    clock.now = issued.not_after + timedelta(days=1)
    proof = expired_proof(key, NODE_C, issued.serial, csr(NODE_C), clock.now)
    real = enrollment.renew_expired
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("transient internal failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(enrollment, "renew_expired", flaky)
    headers = {"content-type": "application/json"}
    first = client.post(
        "/agent/renew/expired", content=proof.model_dump_json(), headers=headers
    )
    assert first.status_code == 503
    again = client.post(
        "/agent/renew/expired", content=proof.model_dump_json(), headers=headers
    )
    assert again.status_code == 200


_GRANT_KEYS = [
    '"grant_token"',
    '"gr\\u0061nt_token"',
    '"grant\\u005ftoken"',
    '"csr"',
    '"grant_tokens"',
]


_TOKEN_TEXT = st.text("ABCxyz019_-", min_size=43, max_size=43).map(json.dumps)


_JSON_LEAF = st.one_of(
    _TOKEN_TEXT,
    st.text(max_size=5).map(json.dumps),
    st.just("null"),
    st.integers().map(str),
    st.just('"\\\\"'),
)


_WHITESPACE = st.sampled_from(["", " ", "\n"])


def _json_containers(inner):
    array = st.lists(inner, max_size=3).map(lambda items: "[" + ",".join(items) + "]")
    obj = st.lists(
        st.tuples(st.sampled_from(_GRANT_KEYS), _WHITESPACE, inner), max_size=4
    ).map(
        lambda pairs: (
            "{" + ",".join(f"{key}{ws}:{ws}{value}" for key, ws, value in pairs) + "}"
        )
    )
    return st.one_of(array, obj)


_ENROLLMENT_JSON = st.recursive(_JSON_LEAF, _json_containers, max_leaves=12)


def _grant_pairs(text: str) -> list[list[tuple[str, object]]] | None:
    """Every object's key/value pairs as json.loads sees them; the root is last."""
    objects: list[list[tuple[str, object]]] = []

    def collect(pairs: list[tuple[str, object]]) -> dict[str, object]:
        objects.append(pairs)
        return dict(pairs)

    try:
        body = json.loads(text, object_pairs_hook=collect)
    except (ValueError, RecursionError):
        return None
    return objects if isinstance(body, dict) else None


@pytest.mark.property
@given(
    _ENROLLMENT_JSON,
    st.one_of(
        st.none(), st.tuples(st.integers(0, 200), st.sampled_from('",:{}[]\\ a'))
    ),
)
@example(
    '{"grant_token":"' + "A" * 43 + '","gr\\u0061nt_token":"' + "B" * 43 + '"}', None
)
@example('[{"grant_token":"' + "A" * 43 + '"}]', None)
def test_enrollment_scan_agrees_with_json_loads(text: str, mutation) -> None:
    if mutation is not None and text:
        index = mutation[0] % len(text)
        text = text[:index] + mutation[1] + text[index + 1 :]
    scan = _scan_enrollment_grants(text.encode("utf-8"))
    assert len(set(scan.tokens)) == len(scan.tokens)
    assert all(_ENROLLMENT_TOKEN.fullmatch(token) for token in scan.tokens)
    objects = _grant_pairs(text)
    if objects is None:
        return
    # The caller refuses a body unless this count is exactly one, so it must
    # equal the root-level grant_token pairs json.loads collapses to one.
    assert scan.top_level_keys == sum(key == "grant_token" for key, _ in objects[-1])
    discovered = {
        value
        for pairs in objects
        for key, value in pairs
        if key == "grant_token"
        and isinstance(value, str)
        and _ENROLLMENT_TOKEN.fullmatch(value)
    }
    assert set(scan.tokens) == discovered


@pytest.mark.property
@given(st.binary(max_size=2048))
@example(b"")
@example(b'{"grant_token":"')
def test_enrollment_scan_never_raises_on_arbitrary_bytes(raw: bytes) -> None:
    scan = _scan_enrollment_grants(raw)
    assert all(_ENROLLMENT_TOKEN.fullmatch(token) for token in scan.tokens)
