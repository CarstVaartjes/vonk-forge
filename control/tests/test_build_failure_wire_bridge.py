"""Native source-fetch failures keep the accepted build owner's retry policy."""

from __future__ import annotations

import ssl
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    RecipeBuildRequest,
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Job,
    User,
)
from vonk_control.recipe_image_availability import RecipeImageAvailabilityError

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_agent_restart_recovery_wire_bridge import (
    _certificate_files,
    _probe_request,
    _run_probe,
    controller,  # noqa: F401
    restart_probe,  # noqa: F401
)
from .test_build_cancellation_recovery import _services


class _SourceBundleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        source_sha256: str,
        status: int,
        body: bytes,
        retry_after: str | None,
    ) -> None:
        super().__init__(address, _SourceBundleHandler)
        self.source_sha256 = source_sha256
        self.status = status
        self.body = body
        self.retry_after = retry_after
        self.requests: list[str] = []


class _SourceBundleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _SourceBundleServer)
        server.requests.append(self.path)
        if self.path != f"/agent/source-bundles/{server.source_sha256}":
            self.send_error(404)
            return
        self.send_response(server.status)
        self.send_header("Content-Length", str(len(server.body)))
        if server.retry_after is not None:
            self.send_header("Retry-After", server.retry_after)
        self.end_headers()
        if server.body:
            self.wfile.write(server.body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _serve_source_bundle(
    certs: dict[str, Path],
    *,
    source_sha256: str,
    status: int,
    body: bytes,
    retry_after: str | None = None,
) -> tuple[_SourceBundleServer, threading.Thread]:
    server = _SourceBundleServer(
        ("127.0.0.1", 0),
        source_sha256=source_sha256,
        status=status,
        body=body,
        retry_after=retry_after,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certs["server_pem"]), str(certs["server_key"]))
    context.load_verify_locations(cafile=str(certs["ca_pem"]))
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _fresh_build_identity(sessions, node_id: str, now: datetime) -> None:
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.last_seen_at = now
        session.add(
            AgentCertificate(
                serial="build-wire-serial",
                node_id=node_id,
                not_before=now - timedelta(minutes=1),
                not_after=now + timedelta(hours=1),
                fingerprint="build-wire-fingerprint",
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id,
            now,
            2 * 1024**4,
            1 * 1024**4,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            (
                "recipe.build.v1",
                "recipe.build.egress-proxy.v1",
                "recipe.image.import.v1",
            ),
            memory_pool="separate",
        )
    )


@pytest.mark.parametrize(
    ("response", "expected_kind", "expected_retry_after"),
    [
        ("unavailable", AgentFailureKind.TEMPORARY_DEPENDENCY, 7),
        ("forbidden", AgentFailureKind.INVALID_AUTHORITY, None),
        ("mismatched", AgentFailureKind.INVALID_CONTRACT, None),
    ],
)
def test_native_source_fetch_failure_reaches_availability_owner(
    tmp_path: Path,
    controller,  # noqa: F811 - imported pytest fixture
    restart_probe: Path,  # noqa: F811 - imported pytest fixture
    response: str,
    expected_kind: AgentFailureKind,
    expected_retry_after: int | None,
) -> None:
    sessions, clock = controller
    with sessions() as session:
        engine = session.get_bind()
    sessions, builds, operations, _storage, _old_now, node_id, revision, plan = (
        _services(tmp_path, engine)
    )
    now = clock.now
    operations._clock = clock
    _fresh_build_identity(sessions, node_id, now)
    with sessions() as session:
        builder = session.get(AgentNode, node_id)
        assert builder is not None
        runtime_identity = PACKAGED_RUNTIME_IDENTITY | {
            "architecture": builder.architecture,
            "semantic_version": builder.semantic_version,
            "build_digest": builder.build_digest,
            "binary_digest": builder.binary_digest,
            "self_test_passed": builder.self_test_passed,
        }
    jobs = AgentJobService(
        sessions,
        clock=clock,
        result_consumer=operations.consume_agent_result,
    )
    operations._agent_jobs = jobs

    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))

    source_sha256 = plan.agent_payload.get("source_bundle_sha256")
    assert isinstance(source_sha256, str)
    source_archive = builds._bundles.get(source_sha256).archive
    status, body, retry_after = {
        "unavailable": (503, b"source registry unavailable", "7"),
        "forbidden": (403, b"source access denied", None),
        # A successful status with the wrong byte count is a terminal protocol
        # response before the probe's deliberately non-running build process.
        "mismatched": (200, source_archive[:-1], None),
    }[response]
    certificate_root = tmp_path / "certificates"
    certificate_root.mkdir()
    certs = _certificate_files(certificate_root)
    source_server, source_thread = _serve_source_bundle(
        certs,
        source_sha256=source_sha256,
        status=status,
        body=body,
        retry_after=retry_after,
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "runtime-images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=clock,
    )
    try:
        service = production.service
        parent = service.start(
            revision.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
        )
        parent_claims = service.claim_pending(limit=1, owner_id="build-wire-owner")
        assert len(parent_claims) == 1 and parent_claims[0].operation_id == parent.id
        service.run_claim(parent_claims[0])

        with sessions() as session:
            build_job = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
        assert build_job is not None
        first_claim = claim_agent(
            jobs,
            node_id,
            "build-wire-serial",
            30,
            capabilities=("agent.runtime.rust.v1", "recipe.build.v1"),
            runtime_identity=runtime_identity,
        )
        assert first_claim is not None
        assert first_claim.job_id == build_job.id
        assert first_claim.operation == "recipe.build.v1"
        accepted_request = RecipeBuildRequest.model_validate_json(
            canonical_message(first_claim.payload)
        )
        assert accepted_request.source_bundle_sha256 == source_sha256

        result = _run_probe(
            restart_probe,
            _probe_request(
                "execute-build",
                first_claim,
                tmp_path / "agent-state",
                source_server,
                certs,
            ),
        )
        assert result.state == "failed"
        failure = AgentFailureResult.model_validate_json(
            canonical_message(result.result)
        )
        assert failure.status == "failed"
        assert failure.error_code == "recipe_build_failed"
        assert failure.stage == "source-bundle-fetch"
        assert failure.failure_kind is expected_kind
        assert failure.retry_after_seconds == expected_retry_after
        assert source_server.requests == [f"/agent/source-bundles/{source_sha256}"]

        # The exact accepted attempt and its real producer result cross the
        # fenced queue boundary before the parent is allowed to classify it.
        jobs.record_result(result)
        with sessions() as session:
            accepted_job = session.get(Job, result.job_id)
            stored_operation = session.get(AgentOperation, result.operation_id)
            assert accepted_job is not None and stored_operation is not None
            assert accepted_job.state == "failed"
            assert stored_operation.state == "failed"
            stored_evidence = accepted_job.result
            assert isinstance(stored_evidence, dict)
            node_evidence = stored_evidence.get("node_evidence")
            assert isinstance(node_evidence, dict)
            owner_failure = AgentFailureResult.model_validate_json(
                canonical_message(node_evidence[node_id])
            )
            assert owner_failure.failure_kind is expected_kind

        observed = service.get(parent.id)
        for _ in range(4):
            if observed.failure is not None and observed.failure.get("code") == (
                "recipe_build_failed"
            ):
                break
            failure_record = observed.failure
            assert isinstance(failure_record, dict)
            retry_time = failure_record.get("retry_time")
            assert isinstance(retry_time, str)
            clock.now = datetime.fromisoformat(retry_time) + timedelta(seconds=1)
            retry_claims = service.claim_pending(
                limit=1, owner_id="build-wire-observer"
            )
            assert len(retry_claims) == 1
            service.run_claim(retry_claims[0])
            observed = service.get(parent.id)

        assert observed.failure is not None
        assert observed.failure.get("code") == "recipe_build_failed"
        assert observed.failure.get("retryable") is (
            expected_kind is AgentFailureKind.TEMPORARY_DEPENDENCY
        )

        if expected_kind is AgentFailureKind.TEMPORARY_DEPENDENCY:
            # The original authorized parent retries automatically with a new
            # fenced child after the transient failure's next check is due.
            assert observed.state == "queued"
            retry_time = observed.failure.get("retry_time")
            assert isinstance(retry_time, str)
            clock.now = datetime.fromisoformat(retry_time) + timedelta(seconds=1)
            retry_parent_claim = service.claim_pending(
                limit=1, owner_id="build-wire-recovery"
            )
            assert len(retry_parent_claim) == 1
            assert retry_parent_claim[0].operation_id == parent.id
            service.run_claim(retry_parent_claim[0])
            with sessions() as session:
                jobs_for_build = tuple(
                    session.scalars(
                        select(Job)
                        .where(Job.kind == "recipe.build.v1")
                        .order_by(Job.created_at, Job.id)
                    )
                )
            assert len(jobs_for_build) == 2
            assert jobs_for_build[0].id == build_job.id
            assert jobs_for_build[1].request_id != jobs_for_build[0].request_id
            second_claim = claim_agent(
                jobs,
                node_id,
                "build-wire-serial",
                30,
                capabilities=("agent.runtime.rust.v1", "recipe.build.v1"),
                runtime_identity=runtime_identity,
            )
            assert second_claim is not None
            assert second_claim.job_id == jobs_for_build[1].id
            assert (
                RecipeBuildRequest.model_validate_json(
                    canonical_message(second_claim.payload)
                )
                == accepted_request
            )
            assert service.get(parent.id).state in {"queued", "running", "partial"}
        else:
            assert observed.state == "failed"
            with pytest.raises(RecipeImageAvailabilityError) as refused:
                service.retry(
                    parent.id,
                    actor="operator",
                    request_id=str(uuid.uuid4()),
                )
            assert refused.value.code == "recipe_image.not_retryable"
            with sessions() as session:
                assert (
                    len(
                        tuple(
                            session.scalars(
                                select(Job).where(Job.kind == "recipe.build.v1")
                            )
                        )
                    )
                    == 1
                )
    finally:
        production.close()
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)
