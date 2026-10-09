"""Catch strictness drift, unsafe integer rounding and diagnostic loss at real consumer seams."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentProgress,
    AgentResult,
    OperationProgress,
)
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_control.operation_api import (
    JobDetailResponse,
    JobProgress,
    OperationApiServices,
    OperationPage,
)
from vonk_control.operation_item_contract import OperationItem, OperationResultFacts

from cluster_profiles.generated_control.api.default import get_job
from cluster_profiles.generated_control.client import AuthenticatedClient
from cluster_profiles.generated_control.models.job_detail_response import (
    JobDetailResponse as GeneratedJobDetailResponse,
)

from .cross_language_consumer_corpus import (
    FENCE,
    MODELS,
    NODE,
    OPERATION,
    agent_envelope,
    corpus,
    diagnostic,
    job_envelope,
)
from .recipe_stop_fixtures import recipe_stop_payload
from .test_agent_restart_recovery_wire_bridge import _certificate_files
from .test_operation_api import EnqueuedJob, Jobs, _client
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def probe() -> Path:
    configured = os.environ.get("VONK_CONSUMER_CORPUS_PROBE")
    if configured:
        return Path(configured)
    if os.environ.get("CI"):
        pytest.fail("hosted consumer corpus requires the actual Rust probe")
    pytest.skip("Rust builds run in the hosted corpus lane")


def rust(
    component: str, text: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(probe()), component, *arguments],
        input=text,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("case", corpus()["cases"], ids=lambda case: case["id"])
def test_shared_raw_document_has_same_acceptance_and_roundtrip(case) -> None:
    if "rust" not in case["consumers"]:
        pytest.skip("document has no Rust API consumer")
    result = rust(case["component"], case["text"])
    assert (result.returncode == 0) == case["accepted"], result.stderr
    if case["accepted"]:
        model = MODELS[case["component"]]
        assert model.model_validate_json(result.stdout) == model.model_validate_json(
            case["normalized_text"]
        )

    else:
        valid = next(
            candidate
            for candidate in corpus()["cases"]
            if candidate["component"] == case["component"]
            and candidate["accepted"]
            and "rust" in candidate["consumers"]
        )
        repaired = rust(valid["component"], valid["text"])
        assert repaired.returncode == 0, repaired.stderr
        model = MODELS[valid["component"]]
        assert model.model_validate_json(repaired.stdout) == model.model_validate_json(
            valid["normalized_text"]
        )


def test_lossy_numeric_mutation_is_detected() -> None:
    # A plausible wrong consumer uses a double for a strict integer counter.
    model = MODELS["OperationProgress"]
    text = '{"phase":"transfer","completed_bytes":9007199254740993}'
    expected = model.model_validate_json(text)
    wrong = json.loads(text)
    wrong["completed_bytes"] = int(float(wrong["completed_bytes"]))
    assert model.model_validate_json(json.dumps(wrong)) != expected
    with pytest.raises(ValidationError):
        model.model_validate_json('{"phase":"transfer","completed_bytes":1.0}')


def job_diagnostics(job: JobDetailResponse) -> FailureDiagnostics:
    assert job.state == "failed" and job.kind == "recipe.stop"
    assert job.targets == [NODE]
    assert job.projection_issue is None
    assert job.operations is not None and len(job.operations) == 1
    assert job.operations[0].id == OPERATION
    assert job.operations[0].node_id == NODE
    assert job.operations[0].state == "failed"
    failure = job.operations[0].failure
    assert isinstance(failure, AgentFailureResult)
    assert failure.diagnostics is not None
    return failure.diagnostics


def api_with_diagnostic(leaf: dict):
    # Only the observation source is injected; production create_app, projection,
    # authentication, response serialization, generated HTTP and CLI all execute.
    jobs = Jobs()
    jobs.job = EnqueuedJob(state="failed", kind="recipe.stop", targets=(NODE,))
    failure = AgentFailureResult(
        status="failed",
        error_code="recipe_stop_failed",
        reason="runtime stop failed",
        diagnostics=FailureDiagnostics.model_validate(leaf),
    )
    item = OperationItem(
        id=OPERATION,
        job_id=jobs.job.id,
        parent_id=jobs.job.id,
        node_id=NODE,
        node_ids=[NODE],
        kind="recipe.stop",
        state="failed",
        attempt=1,
        created_at="2026-10-07T01:00:00Z",
        progress=OperationProgress(phase="stop", completed_bytes=9007199254740993),
        result=OperationResultFacts.of(failure),
        agent_receipt=failure,
        supported_actions=["inspect"],
    )
    services = OperationApiServices(
        agents=lambda: (),
        job_operations=lambda _id, _cursor, _limit: OperationPage(
            (item,), None, JobProgress(completed=0, failed=1, running=0, total=1)
        ),
        resume_job=lambda _id: None,
    )
    return _client(operations=services, jobs=jobs)[:2]


@pytest.mark.lane
def test_actual_asgi_installed_cli_and_generated_http_preserve_same_leaf(
    installed_vonkctl: Path, tmp_path: Path
) -> None:
    leaf = diagnostic()
    leaf["stdout"]["dropped_bytes"] = 9007199254740993
    leaf_text = json.dumps(leaf)
    agent = AgentResult.model_validate_json(agent_envelope(leaf_text))
    assert agent.stored_result().diagnostics == FailureDiagnostics.model_validate_json(
        leaf_text
    )
    api, headers = api_with_diagnostic(leaf)
    with api, _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        response = api.get(f"/api/jobs/{EnqueuedJob.id}", headers=headers)
        assert response.status_code == 200
        raw = JobDetailResponse.model_validate_json(response.content)
        assert job_diagnostics(raw).model_dump(mode="json") == leaf
        client = AuthenticatedClient(
            base_url=url,
            verify_ssl=str(certificate),
            token=headers["Authorization"].removeprefix("Bearer "),
        )
        generated = get_job.sync_detailed(EnqueuedJob.id, client=client)
        assert generated.status_code == 200
        assert isinstance(generated.parsed, GeneratedJobDetailResponse)
        generated_typed = JobDetailResponse.model_validate_json(
            json.dumps(generated.parsed.to_dict())
        )
        assert generated_typed == raw
        completed = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "fleet",
                "progress",
                EnqueuedJob.id,
            ],
            cwd=tmp_path,
            env=_process_environment(tmp_path, url, certificate, headers),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        cli = JobDetailResponse.model_validate_json(completed.stdout)
        assert cli.operations is not None
        assert cli.operations[0].progress is not None
        assert cli.operations[0].progress.completed_bytes == 9007199254740993
        assert job_diagnostics(cli).model_dump(mode="json") == leaf


def test_real_state_writer_restart_replay_shares_leaf_with_job_response(
    tmp_path: Path,
) -> None:
    claim = AgentClaim.model_validate_json(
        json.dumps(
            {
                "fence": FENCE,
                "operation": "recipe.stop",
                "payload": recipe_stop_payload(NODE, plan_digest="a" * 64),
                "deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            }
        )
    )
    result = rust("state", claim.model_dump_json(), str(tmp_path / "state.sqlite"))
    assert result.returncode == 0, result.stderr
    message = AgentResult.model_validate_json(result.stdout)
    diagnostics = message.stored_result().diagnostics
    assert diagnostics is not None
    leaf = diagnostics.model_dump_json()
    job = JobDetailResponse.model_validate_json(job_envelope(leaf))
    assert job_diagnostics(job) == diagnostics
    api, headers = api_with_diagnostic(diagnostics.model_dump(mode="json"))
    with api:
        observed = api.get(f"/api/jobs/{EnqueuedJob.id}", headers=headers)
    assert observed.status_code == 200
    actual = JobDetailResponse.model_validate_json(observed.content)
    assert job_diagnostics(actual) == diagnostics


@pytest.mark.parametrize(
    ("status", "oversized", "case_id"),
    [
        (422, False, "error-422"),
        (422, True, "error-422"),
        (503, False, "error-503"),
        (422, False, "error-422-loc-18446744073709551617"),
        (422, False, "error-422-loc-200-digits"),
        (422, False, "error-422-loc-private-number-object"),
        (200, False, "heartbeat-progress-18446744073709551617"),
        (200, False, "heartbeat-progress-200-digits"),
    ],
)
def test_real_agent_client_reads_bounded_422_and_preserves_503_status(
    tmp_path: Path, status: int, oversized: bool, case_id: str
) -> None:
    selected = next(case for case in corpus()["cases"] if case["id"] == case_id)
    body = selected["text"].encode()
    if oversized:
        body = b" " * (64 * 1024) + body
    if status == 200:
        body = (
            AgentDirective(
                fence=FENCE,
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                cancel_requested=False,
            )
            .model_dump_json()
            .encode()
        )
    certs = _certificate_files(tmp_path)
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (self.path, self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs["server_pem"], certs["server_key"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://localhost:{server.server_port}"
    # AgentConfig owns this on-disk TOML; stdin carries one canonical wire
    # document, never a second handwritten HTTP/configuration envelope.
    config = {
        "enrollment_url": url,
        "controller_url": url,
        "ca_path": str(certs["ca_pem"]),
        "ca_sha256": hashlib.sha256(
            x509.load_pem_x509_certificate(certs["ca_pem"].read_bytes()).public_bytes(
                serialization.Encoding.DER
            )
        ).hexdigest(),
        "data_dir": str(tmp_path),
        "node_id": NODE,
    }
    config_path = tmp_path / "agent.toml"
    config_path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in config.items())
    )
    config_path.chmod(0o600)
    document = (
        selected["text"] if status == 200 else agent_envelope(json.dumps(diagnostic()))
    )
    try:
        result = rust(
            "heartbeat" if status == 200 else "http",
            document,
            str(config_path),
            str(certs["client_pem"]),
            str(certs["chain_pem"]),
            str(certs["client_key"]),
        )
        assert result.returncode == 0, result.stderr
        if status == 200:
            directive = AgentDirective.model_validate_json(result.stdout)
            assert directive == AgentDirective.model_validate_json(body)
            assert len(received) == 1 and received[0][0] == "/agent/heartbeat"
            assert AgentProgress.model_validate_json(
                received[0][1]
            ) == AgentProgress.model_validate_json(selected["text"])
            return
        observed = json.loads(result.stdout)
        assert observed["status"] == status
        if status == 422:
            assert (observed["summary"] is not None) == (
                not oversized and selected["accepted"]
            )
            if not oversized and selected["accepted"]:
                location = json.loads(selected["text"])["issues"][0]["loc"][2]
                # The actual reader bounds summaries; huge loc is either represented
                # faithfully or explicitly clipped by its published summary bound.
                assert str(location)[:40] in observed["summary"]
        else:
            # This production branch classifies headers/status, not body content.
            assert observed["summary"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
