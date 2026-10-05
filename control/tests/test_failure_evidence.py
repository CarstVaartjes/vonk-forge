from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import AgentResult
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_control.agent_jobs import AgentJobService
from vonk_control.failure_evidence import (
    FailureEvidenceBundle,
    FailureEvidenceService,
    collect_failure,
    log_tail,
    safe_text,
    sanitize_diagnostics,
)
from vonk_control.failure_evidence_api import install_failure_evidence_routes
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)

from .agent_fences import fenced_attempt
from .runtime_identity_support import claim_agent
from .test_agent_jobs import NODE_A, NODE_B, Clock, parent

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
COMMIT = "a" * 64
DISTRIBUTION_SUCCESS = {"downloaded_bytes": 200}


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'evidence.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return FailureEvidenceService(sessions, clock=lambda: NOW)


def store_failed_job(service, value: dict[str, object]) -> None:
    """Persist ``value`` as a failed Controller job the download renders from."""
    with service.sessions.begin() as session:
        session.add(
            Job(
                id=value["id"],
                request_id=str(uuid4()),
                kind=value["kind"],
                state="failed",
                actor="test",
                authority_revision="a" * 64,
                targets=value["node_ids"],
                payload_digest="b" * 64,
                payload={},
                result=value["result"],
                current_attempt=value["attempt"],
                created_at=NOW,
                updated_at=NOW,
            )
        )


def rendered(value) -> str:
    return collect_failure(value, now=NOW).model_dump_json()


def item(kind="recipe.build.v1", attempt=1):
    return {
        "id": str(uuid4()),
        "attempt": attempt,
        "kind": kind,
        "node_ids": ["spk_" + "a" * 32],
        "updated_at": NOW.isoformat(),
        "progress": {"phase": "prepare"},
        "result": {
            "reason": "Podman could not mount proc",
            "diagnostic": "proc-mount-denied",
            "stderr": "Error mounting /proc: permission denied",
        },
    }


@pytest.mark.parametrize(
    "kind",
    [
        "recipe.build.v1",
        "artifact.distribution.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "agent.upgrade.v1",
        "model-cache.download",
    ],
)
def test_every_operation_collector_keeps_phase_and_code(kind):
    value = item(kind)
    before = copy.deepcopy(value)
    bundle = collect_failure(value, now=NOW)
    assert bundle.diagnostics.phase == "prepare"
    assert bundle.diagnostics.category == "platform-policy"
    assert bundle.error_code == "operation_failed"
    assert bundle.detail == "proc-mount-denied"
    assert "permission denied" in bundle.diagnostics.stderr.text
    assert bundle.context.node_ids == value["node_ids"]
    assert value == before


@pytest.mark.parametrize(
    "code,expected",
    [
        ("permission-denied", "platform-policy"),
        ("insufficient-storage", "capacity"),
        ("network-unreachable", "network"),
        ("digest-mismatch", "digest"),
        ("deadline-exceeded", "timeout"),
        ("engine-exit", "runtime"),
    ],
)
def test_failure_classification_uses_codes(code, expected):
    value = item()
    value["result"]["diagnostic"] = code
    assert collect_failure(value, now=NOW).diagnostics.category == expected


def test_large_fleet_keeps_bounded_evidence():
    value = item()
    value["node_ids"] = [f"spk_{index:032x}" for index in range(1024)]
    bundle = collect_failure(value, now=NOW)
    assert bundle.context.node_ids == value["node_ids"][:128]


def test_redaction_handles_adversarial_values_in_the_download(service):
    value = item()
    value["result"].update(
        {
            "stderr": "\n".join(
                [
                    "Authorization: Bearer super-sensitive",
                    'api_key="quoted multi word value"',
                    "Cookie: sessionid=hidden",
                    "https://user:password@example.test/blob?X-Amz-Signature=signed-secret",
                    "-----BEGIN PRIVATE KEY-----",
                    "A" * 64,
                    "-----END PRIVATE KEY-----",
                    "permission denied mounting proc",
                ]
            ),
            "nested": {"environment": {"HF_TOKEN": "sensitive"}},
        }
    )
    content = rendered(value)
    for secret in (
        "super-sensitive",
        "quoted multi word",
        "sessionid=hidden",
        "signed-secret",
        "A" * 64,
    ):
        assert secret not in content
    assert "permission denied" in content


def test_redaction_keeps_a_constraint_violation_that_names_an_authorization_table() -> (
    None
):
    """The line filter redacts credential values, not the topic.

    A bare ``authorization`` alternative replaced any diagnostic line that
    mentioned ``runtime_image_authorizations``, which is exactly the constraint
    violation an operator has to read to repair the database.
    """

    violation = (
        'IntegrityError: null value in column "receipt_id" of relation '
        '"runtime_image_authorizations" violates not-null constraint'
    )
    assert safe_text(violation) == violation
    header = safe_text("Authorization: Bearer super-sensitive")
    assert "[redacted diagnostic line]" == header
    assert "super-sensitive" not in header


def test_a_traceback_frame_path_is_not_redacted() -> None:
    """An opaque value is a whole token, not a run inside a longer word.

    The opaque-value rule matched any 40-character run of base64-ish characters
    anywhere in a line, so every Python frame arrived as
    ``python3.[redacted opaque value].py`` and the traceback named no module at
    all -- the one thing a failed workload has to say.
    """

    frame = (
        '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/'
        'async_llm.py", line 220, in from_vllm_config'
    )
    assert safe_text(frame) == frame
    opaque = "A" * 64
    assert opaque not in safe_text(f"signature={opaque}")


def test_a_configuration_line_that_names_tokens_is_not_redacted() -> None:
    """The line filter redacts credential values, not the topic's name.

    A bare ``token`` alternative replaced every line that mentioned
    ``num_speculative_tokens`` with ``[redacted diagnostic line]``, which removed
    the launch configuration a failed workload is diagnosed from.
    """

    configuration = (
        "speculative-config {num_speculative_tokens: 7, model: /models/drafter}"
    )
    assert safe_text(configuration) == configuration
    assert safe_text("tokenizer_config.json was read") == (
        "tokenizer_config.json was read"
    )
    assert safe_text("HF_TOKEN=hunter2") == "[redacted diagnostic line]"
    assert safe_text("token: hunter2") == "[redacted diagnostic line]"


def test_sanitize_diagnostics_keeps_the_end_when_redaction_expands_a_tail() -> None:
    """Redaction grows a tail past the bound, so the bound belongs at its front.

    ``[redacted diagnostic line]`` is longer than the credential line it
    replaces, so a stream that is mostly credential lines grows past the
    declared bound. A head slice then kept the redacted noise and discarded the
    failure that ended the stream -- the opposite of what a tail field means.
    """

    text = "token: v\n" * 150 + "the container exited here\n"
    assert len(text.encode()) <= 2048
    diagnostics = FailureDiagnostics.model_validate(
        {
            "schema_version": 1,
            "collected_at": NOW.isoformat(),
            "phase": "recipe.start",
            "category": "runtime",
            "stdout": {
                "text": "",
                "truncated": False,
                "dropped_bytes": 0,
                "dropped_lines": 0,
            },
            "stderr": {
                "text": text,
                "truncated": False,
                "dropped_bytes": 0,
                "dropped_lines": 0,
            },
            "versions": [],
            "sandbox": [],
            "storage": [],
            "preflight": [],
            "collector_errors": [],
        }
    )
    cleaned = sanitize_diagnostics(diagnostics)
    assert cleaned.stderr.text.endswith("the container exited here")
    assert len(cleaned.stderr.text.encode()) <= 2048
    assert cleaned.stderr.truncated
    assert (cleaned.stderr.dropped_bytes or 0) > 0
    assert "token: v" not in cleaned.stderr.text


def test_ring_buffer_preserves_last_lines_and_reports_loss():
    tail = log_tail("noise\n" * 20000 + "last useful error\n")
    assert tail.text.endswith("last useful error")
    dropped_bytes = tail.dropped_bytes
    dropped_lines = tail.dropped_lines
    assert isinstance(dropped_bytes, int) and isinstance(dropped_lines, int)
    assert tail.truncated and dropped_bytes > 0 and dropped_lines > 0
    assert len(tail.text.encode()) <= 2048
    assert len(tail.text.splitlines()) <= 32


def test_collector_failure_preserves_original_result_and_is_separate(
    service, monkeypatch
):
    value = item()
    before = copy.deepcopy(value)

    def fail(*_args, **_kwargs):
        raise RuntimeError("secret diagnostic error")

    store_failed_job(service, value)
    monkeypatch.setattr("vonk_control.failure_evidence.collect_failure", fail)
    bundle = service.read(value["id"], 1)
    assert bundle.collector_errors == ["collector-failed"]
    assert bundle.summary == value["result"]["reason"]
    assert value == before
    assert "secret diagnostic error" not in bundle.model_dump_json()


def test_offline_node_uses_durable_evidence_without_probe(service):
    value = item("recipe.start")
    store_failed_job(service, value)
    bundle = service.read(value["id"], 1)
    assert bundle.diagnostics.collector_errors == ["agent-observations-unavailable"]
    assert bundle.context.node_ids == value["node_ids"]


def test_typed_agent_diagnostics_retained_and_resanitized():
    value = item()
    diagnostics = collect_failure(value, now=NOW).diagnostics.model_dump(mode="json")
    diagnostics["sandbox"] = [{"name": "NoNewPrivileges", "value": "yes"}]
    diagnostics["storage"] = [{"name": "free-bytes", "value": "1024"}]
    diagnostics["versions"] = [{"name": "kernel", "value": "6.12"}]
    diagnostics["stderr"]["text"] = "token=should-never-persist"
    value["result"]["diagnostics"] = FailureDiagnostics.model_validate(
        diagnostics
    ).model_dump(mode="json")
    bundle = collect_failure(value, now=NOW)
    assert "should-never-persist" not in bundle.model_dump_json()
    assert bundle.diagnostics.sandbox[0].value == "yes"
    assert bundle.diagnostics.storage[0].value == "1024"


def test_evidence_download_is_authenticated_exact_attempt_and_stable(service):
    value = item()
    store_failed_job(service, value)
    app = FastAPI()

    def actor(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test":
            raise HTTPException(401, "authentication required")
        return "viewer"

    install_failure_evidence_routes(
        app, actor_dependency=Depends(actor), service=service
    )
    client = TestClient(app)
    url = f"/api/operations/{value['id']}/evidence?attempt=1"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert (
        FailureEvidenceBundle.model_validate_json(response.content).context.attempt == 1
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    assert (
        response.content
        == client.get(url, headers={"Authorization": "Bearer test"}).content
    )
    assert (
        client.get(
            url.replace("attempt=1", "attempt=2"),
            headers={"Authorization": "Bearer test"},
        ).status_code
        == 404
    )
    # A job that has not failed renders nothing, rather than an invented failure.
    with service.sessions.begin() as session:
        session.get(Job, value["id"]).state = "succeeded"
    assert client.get(url, headers={"Authorization": "Bearer test"}).status_code == 404


def test_composed_controller_exposes_exact_download_on_operation_projection(service):
    from vonk_control.api import create_app
    from vonk_control.auth import Actor, TokenCodec
    from vonk_control.operation_api import (
        OperationApiServices,
        OperationListPage,
        OperationPage,
    )

    from .test_api import Jobs

    value: dict[str, object] = {
        **item(),
        "state": "failed",
        "created_at": NOW.isoformat(),
    }
    # The composed API consumes the same nested diagnostics as AgentResult.
    diagnostics = collect_failure(value, now=NOW).diagnostics
    result = value["result"]
    assert isinstance(result, dict)
    result.pop("stderr")
    result["diagnostics"] = diagnostics.model_dump(mode="json")
    store_failed_job(service, value)
    codec = TokenCodec(b"k" * 32)

    def job_operations(
        _job_id: str, _operation_cursor: str | None, _limit: int
    ) -> OperationPage:
        raise AssertionError("job operations are not projected in this test")

    operations = OperationApiServices(
        agents=list,
        job_operations=job_operations,
        resume_job=lambda _: None,
        get_operation=lambda _: value,
        list_operations=lambda *_: OperationListPage([value], None, 1),
    )
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        now=lambda: 10,
        operations=operations,
        failure_evidence=service,
    )
    client = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(f"/api/operations/{value['id']}", headers=headers)
    assert response.status_code == 200
    download = response.json()["evidence_download"]
    assert download == {"href": f"/api/operations/{value['id']}/evidence?attempt=1"}
    listing = client.get("/api/operations", headers=headers)
    assert listing.json()["operations"][0]["evidence_download"] == download
    assert client.get(download["href"]).status_code == 401
    content = client.get(download["href"], headers=headers)
    assert content.status_code == 200
    assert (
        FailureEvidenceBundle.model_validate_json(content.content).context.attempt == 1
    )


def test_secret_control_characters_cannot_evade_redaction():
    value = item()
    value["result"]["stderr"] = (
        "Auth\x00orization: Bearer hidden-value\n\x1b[31mpermission denied\n"
    )
    value["result"]["tok\u200ben"] = "hidden-in-obfuscated-key"
    content = rendered(value)
    assert "hidden-value" not in content
    assert "hidden-in-obfuscated-key" not in content
    assert "\\u001b" not in content
    assert "permission denied" in content


def _agent_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-evidence.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def durable_evidence(tmp_path):
    """A real agent-job store and the real evidence collector on one database."""
    sessions = _agent_sessions(tmp_path)
    clock = Clock()
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    workload_intent_ordinal=1,
                    architecture="linux-arm64",
                    semantic_version="1.0.0",
                    build_digest="sha256:" + "f" * 64,
                    binary_digest="f" * 64,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    jobs = AgentJobService(sessions, clock=clock)
    return jobs, sessions, clock, FailureEvidenceService(sessions, clock=clock)


def _claim_envelope(claim) -> dict[str, object]:
    return {key: claim.model_dump(mode="json")[key] for key in ("fence",)}


def _enqueue_distribution(jobs, sessions, clock):
    return jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        {"plan_digest": COMMIT},
    )


def test_lease_expired_attempt_keeps_its_receipt_after_a_later_attempt(
    durable_evidence,
):
    """A lapse's own late receipt survives the attempt that supersedes it.

    Wrong implementation caught: the collector selected attempt states
    ``failed``/``waiting-for-operator`` only, so an attempt that lapsed its
    lease stopped earning evidence the moment a later attempt existed, even
    though the attempt kept the agent's own late failure result.  That is the
    recorded ``7c2ae819`` distribution, whose failed first attempt became
    unrecoverable; reading it back raised ``KeyError`` for a retained receipt.
    """

    jobs, sessions, clock, evidence = durable_evidence
    operation = _enqueue_distribution(jobs, sessions, clock)
    first = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert first is not None and fenced_attempt(sessions, first).attempt == 1
    # The agent's own failure arrives after the lease lapsed.  The Controller
    # keeps it on the expired attempt rather than discarding it.
    clock.advance(seconds=31)
    late = AgentResult.model_validate_json(
        json.dumps(
            {
                **_claim_envelope(first),
                "state": "failed",
                "result": {
                    "status": "failed",
                    "error_code": "artifact_distribution_failed",
                    "reason": "attempt one transport refused",
                    "failure_kind": "temporary-dependency",
                },
            }
        )
    )
    assert jobs.record_late_result(late) is True
    with sessions() as session:
        assert session.get(AgentOperation, operation.id).state == "waiting-for-operator"
    # A later attempt supersedes the lapsed one and succeeds.
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        stored.next_action_at = clock.now
    clock.advance(seconds=1)
    second = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert second is not None and fenced_attempt(sessions, second).attempt == 2
    jobs.succeed(second, DISTRIBUTION_SUCCESS)

    bundle = evidence.read(operation.id, 1)
    assert bundle.context.attempt == 1
    assert bundle.error_code == "artifact_distribution_failed"
    assert "attempt one transport refused" in bundle.summary


def test_superseded_lease_lapse_without_a_receipt_is_not_fabricated(tmp_path):
    """A lapse that left no receipt stays honestly unavailable.

    Wrong implementation caught: a collector that treated every ``expired``
    attempt as a failure would narrate a superseded lapse from the operation's
    current status reason, which by then describes the later attempt.  The
    attempt row owns no receipt, so nothing may be reported for it.
    """

    sessions = _agent_sessions(tmp_path)
    operation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=str(uuid4()),
                node_id=NODE_A,
                kind=ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
                payload_digest="b" * 64,
                payload={},
                authority_revision=COMMIT,
                state="running",
                status_reason="attempt 2 is running",
                current_attempt=2,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                id=str(uuid4()),
                operation_id=operation_id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=NOW,
                agent_certificate_serial="test-serial",
                state="expired",
                progress={"phase": "copying"},
                result=None,
            )
        )
    evidence = FailureEvidenceService(sessions, clock=lambda: NOW)
    with pytest.raises(KeyError):
        evidence.read(operation_id, 1)


def test_a_parked_lease_lapse_is_retained_from_the_controllers_own_reason(tmp_path):
    """A parked lapse with no receipt keeps the Controller's own account.

    Wrong implementation caught: the collector selected the ``failed`` and
    ``waiting-for-operator`` attempt states only, so an order parked by a lease
    lapse -- the attempt ``expired`` with no receipt, the operation
    ``waiting-for-operator`` -- earned no evidence at all even though the
    Controller's own status reason names the exact lapse.  That is the recorded
    ``7c2ae819`` shape for as long as the order waited for a later attempt.
    """

    sessions = _agent_sessions(tmp_path)
    operation_id = str(uuid4())
    reason = (
        "attempt 1 lease expired; lease deadline 2026-09-08T12:00:00+00:00; "
        "last accepted contact never observed; "
        "expired at 2026-09-08T12:01:00+00:00; the effect is unobserved"
    )
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=str(uuid4()),
                node_id=NODE_A,
                kind=ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
                payload_digest="b" * 64,
                payload={},
                authority_revision=COMMIT,
                state="waiting-for-operator",
                status_reason=reason,
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                id=str(uuid4()),
                operation_id=operation_id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=NOW,
                agent_certificate_serial="test-serial",
                state="expired",
                progress={"phase": "copying"},
                result=None,
            )
        )
    evidence = FailureEvidenceService(sessions, clock=lambda: NOW)
    bundle = evidence.read(operation_id, 1)
    assert bundle.context.attempt == 1
    # The Controller's own record of the lapse is the only narrative there is,
    # and no agent refusal is claimed in its place.
    assert "lease expired" in bundle.summary
    assert bundle.error_code == "operation_failed"


def test_download_is_named_only_for_a_stored_failed_attempt(service):
    value = {**item(), "state": "failed"}
    assert "evidence_download" not in service.decorate(value)
    store_failed_job(service, value)
    assert service.decorate(value)["evidence_download"] == {
        "href": f"/api/operations/{value['id']}/evidence?attempt=1"
    }
    assert "evidence_download" not in service.decorate({**value, "attempt": 2})
    assert "evidence_download" not in service.decorate({**value, "state": "running"})
