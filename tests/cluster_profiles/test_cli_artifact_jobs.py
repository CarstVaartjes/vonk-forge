from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from email.message import Message
from pathlib import Path

import pytest
from test_control_client_requests import _artifact_job_response, _Response, _token

from cluster_profiles import cli
from cluster_profiles.cli_artifact_jobs import _may_have_completed
from cluster_profiles.cli_render import render_payload
from cluster_profiles.control_client import (
    ControlClient,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlResponseTooLarge,
    ControlTransportError,
)
from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

RUN_ID = "00000000-0000-4000-8000-000000000002"
JOB_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000004"
CREATE_KEY = "00000000-0000-4000-8000-000000000003"
SUBMIT_KEY = "00000000-0000-4000-8000-000000000005"
CANCEL_KEY = "00000000-0000-4000-8000-000000000006"
OUTPUT_LIMITS = {
    "max_files": 1,
    "max_file_bytes": 1,
    "max_total_bytes": 1,
    "allowed_media_types": ["image/png"],
}


def _job(
    *,
    state: str = "draft",
    declarations: list[dict[str, object]] | None = None,
    uploaded: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    result = copy.deepcopy(_artifact_job_response())
    expected = declarations or []
    input_total_bytes = 0
    for item in expected:
        size = item.get("size_bytes")
        if type(size) is int:
            input_total_bytes += size
    result.update(
        {
            "id": JOB_ID,
            "run_id": RUN_ID,
            "state": state,
            "timeout_seconds": 1,
            "input_declarations": copy.deepcopy(expected),
            "input_files": copy.deepcopy(uploaded or []),
            "input_total_bytes": input_total_bytes,
            "output_limits": copy.deepcopy(OUTPUT_LIMITS),
        }
    )
    if state not in {"draft", "ready"}:
        result["operation_id"] = OPERATION_ID
        result["submit_request_id"] = SUBMIT_KEY
    return result


def _capabilities(*, maximum_input_file_bytes: int = 64) -> dict[str, object]:
    return {
        "schema_version": 1,
        "storage": {
            "in_flight_uploads": 0,
            "max_stored_bytes": 4096,
            "remaining_bytes": 4096,
            "reserved_bytes": 0,
            "used_bytes": 0,
        },
        "transport": {
            "max_input_file_bytes": maximum_input_file_bytes,
            "max_input_files": 4,
            "max_input_total_bytes": 128,
            "max_output_file_bytes": 64,
            "max_output_files": 4,
            "max_output_total_bytes": 128,
            "max_timeout_seconds": 60,
            "reserved_input_names": ["manifest.json"],
        },
    }


class ArtifactJobClient:
    request_timeout_seconds = 15.0

    def __init__(self) -> None:
        self.capabilities = _capabilities()
        self.job: dict[str, object] | None = None
        self.calls: list[tuple[str, str, object, object]] = []
        self.upload_calls: list[str] = []
        self.download_calls: list[str] = []
        self.lose_create_response = False
        self.fail_create_lookup = False
        self.lose_submit_response = False
        self.ignore_submit_key_mismatch = False
        self.lose_cancel_response = False
        self.fail_input_name: str | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (method, path, copy.deepcopy(payload), copy.deepcopy(extra_headers))
        )
        if path == "/api/artifact-jobs/capabilities":
            return copy.deepcopy(self.capabilities)
        if path == f"/api/recipe/runs/{RUN_ID}/artifact-jobs" and method == "GET":
            return {"jobs": [copy.deepcopy(self.job)] if self.job else []}
        if path == f"/api/recipe/runs/{RUN_ID}/artifact-jobs" and method == "POST":
            if self.job is None:
                if payload is None:
                    raise AssertionError("create request has no body")
                raw_declarations = payload.get("inputs")
                if not isinstance(raw_declarations, list):
                    raise AssertionError("create request has no input declarations")
                declarations: list[dict[str, object]] = []
                for item in raw_declarations:
                    if not isinstance(item, dict) or any(
                        not isinstance(key, str) for key in item
                    ):
                        raise AssertionError("input declaration is not an object")
                    declarations.append({key: value for key, value in item.items()})
                self.job = _job(declarations=declarations)
            if self.lose_create_response:
                self.lose_create_response = False
                raise ControlTransportError("response was lost after create acceptance")
            return copy.deepcopy(self.job)
        if path.startswith("/api/artifact-jobs/requests/"):
            if self.fail_create_lookup:
                self.fail_create_lookup = False
                raise ControlTransportError("lookup temporarily unavailable")
            if self.job is None:
                raise ControlNotFound(404, "artifact draft not found")
            return copy.deepcopy(self.job)
        if self.job is None:
            raise ControlNotFound(404, "artifact job not found")
        if path == f"/api/artifact-jobs/{JOB_ID}/submit" and method == "POST":
            if extra_headers is None:
                raise AssertionError("submit request is missing its identity")
            request_id = extra_headers.get("X-Request-ID")
            if not isinstance(request_id, str):
                raise AssertionError("submit request has no key")
            if isinstance(self.job.get("operation_id"), str):
                if (
                    request_id != self.job.get("submit_request_id")
                    and not self.ignore_submit_key_mismatch
                ):
                    raise ControlHTTPError(409, "different request identity")
                return copy.deepcopy(self.job)
            self.job["state"] = "queued"
            self.job["operation_id"] = OPERATION_ID
            self.job["submit_request_id"] = request_id
            if self.lose_submit_response:
                self.lose_submit_response = False
                raise ControlTransportError("submit response was lost")
            return copy.deepcopy(self.job)
        if path == f"/api/artifact-jobs/{JOB_ID}/cancel" and method == "POST":
            if payload is None or extra_headers is None:
                raise AssertionError("cancel request is missing its body or key")
            request_id = extra_headers.get("X-Request-ID")
            reason = payload.get("reason")
            if not isinstance(request_id, str) or not isinstance(reason, str):
                raise AssertionError("cancel request is missing its identity or reason")
            evidence = {
                "cancel_request_id": request_id,
                "cancel_reason": reason,
            }
            self.job["state"] = "cancelling"
            self.job["result_evidence"] = evidence
            self.job["status_reason"] = reason
            if self.lose_cancel_response:
                self.lose_cancel_response = False
                raise ControlTransportError("cancel response was lost")
            return copy.deepcopy(self.job)
        if path == f"/api/artifact-jobs/{JOB_ID}/finalize" and method == "POST":
            self.job["state"] = "ready"
            return copy.deepcopy(self.job)
        if path == f"/api/artifact-jobs/{JOB_ID}/result" and method == "GET":
            return copy.deepcopy(self.job)
        if path == f"/api/artifact-jobs/{JOB_ID}" and method == "GET":
            return copy.deepcopy(self.job)
        raise AssertionError(f"unexpected API call: {method} {path}")

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]:
        name = path.rsplit("/", 1)[-1]
        self.upload_calls.append(name)
        if name == self.fail_input_name:
            self.fail_input_name = None
            raise ControlTransportError("input transfer interrupted")
        content = source.read_bytes()
        assert len(content) == expected_size
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        assert self.job is not None
        declarations = self.job.get("input_declarations")
        if not isinstance(declarations, list):
            raise TypeError("job has no input declarations")
        declaration = next(
            (
                item
                for item in declarations
                if isinstance(item, dict) and item.get("name") == name
            ),
            None,
        )
        if declaration is None:
            raise AssertionError("job has no matching input declaration")
        input_files = self.job.get("input_files")
        if not isinstance(input_files, list):
            raise TypeError("job has no uploaded-input collection")
        input_files.append(copy.deepcopy(declaration))
        return copy.deepcopy(self.job)

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]:
        self.download_calls.append(path)
        assert not overwrite
        destination.write_bytes(b"x" * expected_size)
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha256
        return {
            "destination": str(destination),
            "media_type": media_type,
            "size_bytes": expected_size,
            "sha256": expected_sha256,
        }


def _binding(
    path: Path,
    files: dict[str, bytes],
    *,
    output_limits: dict[str, object] | None = None,
) -> Path:
    paths: dict[str, str] = {}
    declarations: list[dict[str, object]] = []
    for name, content in files.items():
        (path.parent / name).write_bytes(content)
        paths[name] = name
        declarations.append({"slot": "input", "name": name, "media_type": "image/png"})
    path.write_text(
        json.dumps(
            {
                "create": {
                    "interface": "image-job",
                    "parameters": {},
                    "inputs": declarations,
                    "output_limits": output_limits or OUTPUT_LIMITS,
                    "timeout_seconds": 1,
                },
                "input_paths": paths,
            }
        ),
        encoding="utf-8",
    )
    return path


def _declarations(files: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {
            "slot": "input",
            "name": name,
            "media_type": "image/png",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in files.items()
    ]


def _succeeded_job(
    output_files: list[dict[str, object]] | None = None,
    *,
    output_limits: dict[str, object] | None = None,
) -> dict[str, object]:
    job = _job(state="succeeded")
    job["output_manifest_sha256"] = "c" * 64
    job["output_files"] = copy.deepcopy(
        output_files
        or [
            {
                "name": "result.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        ]
    )
    job["output_limits"] = copy.deepcopy(output_limits or OUTPUT_LIMITS)
    job["result_evidence"] = {"elapsed_milliseconds": 1}
    return job


def test_installed_cli_process_exposes_the_complete_artifact_job_surface() -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cluster_profiles.cli import main; raise SystemExit(main())",
            "recipe",
            "job",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert process.returncode == 0
    for action in (
        "list",
        "create",
        "upload",
        "submit",
        "detail",
        "cancel",
        "download",
    ):
        assert action in process.stdout


def test_cli_human_artifact_job_detail_uses_job_identity_and_reconnect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    declaration = {
        "slot": "prompt",
        "name": "prompt.txt",
        "media_type": "text/plain",
        "size_bytes": 6,
        "sha256": hashlib.sha256(b"prompt").hexdigest(),
    }
    client = ArtifactJobClient()
    client.job = _job(
        state="running",
        declarations=[declaration],
        uploaded=[declaration],
    )
    client.job["status_reason"] = "Waiting for the assigned Spark to finish."

    status = cli.main(
        ("recipe", "job", "detail", JOB_ID),
        control_client=client,
    )

    output = capsys.readouterr().out
    assert status == 0
    assert f"Artifact job: {JOB_ID}" in output
    assert f"Run: {RUN_ID}" in output
    assert "State: running" in output
    assert "Reason: Waiting for the assigned Spark to finish." in output
    assert "Inputs: 1 declared, 1 uploaded" in output
    assert "Input: prompt.txt" in output
    assert "Input state: uploaded" in output
    assert f"Operation: {OPERATION_ID}" in output
    assert "Result files: unavailable until job succeeds" in output
    assert f"Reconnect: vonkctl recipe job detail {JOB_ID} --follow" in output
    assert "recipe progress" not in output


@pytest.mark.parametrize("action", ("create", "upload", "submit", "cancel"))
def test_cli_human_artifact_job_mutations_name_the_current_action_and_job(
    action: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    declaration = _declarations({"prompt.png": b"prompt"})[0]
    binding = _binding(tmp_path / "job.json", {"prompt.png": b"prompt"})
    client = ArtifactJobClient()
    if action == "create":
        arguments = ("recipe", "job", "create", "--run", RUN_ID, "--file", str(binding))
        request_id_factory = lambda: CREATE_KEY
    elif action == "upload":
        client.job = _job(
            state="ready",
            declarations=[declaration],
            uploaded=[declaration],
        )
        arguments = ("recipe", "job", "upload", JOB_ID, "--file", str(binding))
        request_id_factory = None
    elif action == "submit":
        client.job = _job(state="ready")
        arguments = ("recipe", "job", "submit", JOB_ID)
        request_id_factory = lambda: SUBMIT_KEY
    else:
        client.job = _job(state="queued")
        arguments = (
            "recipe",
            "job",
            "cancel",
            JOB_ID,
            "--yes",
            "--reason",
            "stop now",
        )
        request_id_factory = lambda: CANCEL_KEY

    status = cli.main(
        arguments,
        control_client=client,
        request_id_factory=request_id_factory,
    )

    output = capsys.readouterr().out
    assert status == 0
    assert f"Artifact job: {JOB_ID}" in output
    assert f"Command: {action}" in output
    assert "State:" in output
    if action == "cancel":
        assert "Reason: stop now" in output


def test_cli_human_artifact_job_list_reports_empty_and_present_jobs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ArtifactJobClient()
    empty = cli.main(
        ("recipe", "job", "list", "--run", RUN_ID),
        control_client=client,
    )
    empty_output = capsys.readouterr().out

    client.job = _job(state="ready")
    listed = cli.main(
        ("recipe", "job", "list", "--run", RUN_ID),
        control_client=client,
    )
    listed_output = capsys.readouterr().out

    assert empty == listed == 0
    assert "Artifact jobs: 0" in empty_output
    assert "No artifact jobs for this run." in empty_output
    assert f"Artifact job: {JOB_ID}" in listed_output
    assert "State: ready" in listed_output


def test_cli_human_artifact_job_download_shows_verified_local_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    client = ArtifactJobClient()
    client.job = _succeeded_job()

    status = cli.main(
        (
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )

    output = capsys.readouterr().out
    digest = hashlib.sha256(b"x").hexdigest()
    assert status == 0
    assert f"Artifact job: {JOB_ID}" in output
    assert "State: succeeded" in output
    assert "Verified output files: 1" in output
    assert f"Verified path: {output_directory / 'result.png'}" in output
    assert "Verified size: 1 B" in output
    assert f"Verified SHA-256: {digest}" in output


def test_cli_human_artifact_job_empty_success_is_not_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_payload(
        {
            "job_id": JOB_ID,
            "state": "succeeded",
            "output_manifest_sha256": "c" * 64,
            "files": [],
            "total_bytes": 0,
        },
        "recipe",
        artifact_job_action="download",
    )

    output = capsys.readouterr().out
    assert "State: succeeded" in output
    assert "Result files: none (job succeeded with an empty result)." in output
    assert "unavailable" not in output


@pytest.mark.parametrize(
    ("status", "body", "expected_ambiguous"),
    [
        (409, b'{"unexpected": true}', False),
        (409, b"x" * (MAX_CONTROL_DOCUMENT_BYTES + 1), False),
        (503, b'{"unexpected": true}', True),
        (202, b"{}", True),
    ],
)
def test_artifact_retry_classifier_respects_http_status_on_invalid_response(
    tmp_path: Path,
    status: int,
    body: bytes,
    expected_ambiguous: bool,
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"

    def opener(*_args, **_kwargs):
        if status < 400:
            return _Response(status, {})
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/submit",
            status,
            "invalid response",
            headers,
            io.BytesIO(body),
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    with pytest.raises((ControlMalformedResponse, ControlResponseTooLarge)) as raised:
        client.request(
            "POST",
            "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/submit",
            extra_headers={"X-Request-ID": SUBMIT_KEY},
        )

    assert raised.value.context is not None
    assert raised.value.context.http_status == status
    assert _may_have_completed(raised.value) is expected_ambiguous


@pytest.mark.parametrize(
    ("status", "body", "expected_ambiguous"),
    [
        (409, b'{"unexpected": true}', False),
        (409, b"x" * (MAX_CONTROL_DOCUMENT_BYTES + 1), False),
        (200, b"{}", True),
    ],
)
def test_artifact_upload_retry_classifier_retains_malformed_response_status(
    tmp_path: Path,
    status: int,
    body: bytes,
    expected_ambiguous: bool,
) -> None:
    source = tmp_path / "input.bin"
    content = b"input"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    headers = Message()
    headers["Content-Type"] = "application/json"

    def opener(*_args, **_kwargs):
        if status < 400:
            return _Response(status, {})
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/inputs/input.bin",
            status,
            "invalid response",
            headers,
            io.BytesIO(body),
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    with pytest.raises((ControlMalformedResponse, ControlResponseTooLarge)) as raised:
        client.upload_file(
            "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/inputs/input.bin",
            source,
            media_type="application/octet-stream",
            expected_sha256=digest,
            expected_size=len(content),
        )

    assert raised.value.context is not None
    assert raised.value.context.http_status == status
    assert _may_have_completed(raised.value) is expected_ambiguous


def test_cli_create_reconciles_lost_receipt_and_never_submits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding(tmp_path / "job.json", {"input.png": b"x"})
    client = ArtifactJobClient()
    client.lose_create_response = True

    result = cli.main(
        (
            "recipe",
            "job",
            "create",
            "--run",
            RUN_ID,
            "--file",
            str(binding),
        ),
        control_client=client,
        request_id_factory=lambda: CREATE_KEY,
    )

    assert result == 0
    assert client.job is not None and client.job["state"] == "ready"
    assert (
        len(
            [
                call
                for call in client.calls
                if call[0] == "POST" and call[1].endswith("/artifact-jobs")
            ]
        )
        == 2
    )
    assert not any(call[1].endswith("/submit") for call in client.calls)
    assert all(
        call[3] == {"X-Request-ID": CREATE_KEY}
        for call in client.calls
        if call[0] == "POST" and call[1].endswith("/artifact-jobs")
    )
    assert "Draft ID:" in capsys.readouterr().err


def test_cli_retains_create_key_when_lookup_is_unavailable_for_a_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding(tmp_path / "job.json", {"input.png": b"x"})
    client = ArtifactJobClient()
    client.lose_create_response = True
    client.fail_create_lookup = True

    uncertain = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "create",
            "--run",
            RUN_ID,
            "--file",
            str(binding),
        ),
        control_client=client,
        request_id_factory=lambda: CREATE_KEY,
    )
    uncertain_document = json.loads(capsys.readouterr().out)

    assert uncertain == 2
    assert uncertain_document["request_key"] == CREATE_KEY
    assert "--request-key" in uncertain_document["reconcile"]["operation"]
    assert client.job is not None and client.job["state"] == "draft"

    retried = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "create",
            "--run",
            RUN_ID,
            "--file",
            str(binding),
            "--request-key",
            CREATE_KEY,
        ),
        control_client=client,
    )
    retry_document = json.loads(capsys.readouterr().out)

    assert retried == 0
    assert retry_document["id"] == JOB_ID
    assert retry_document["state"] == "ready"
    create_calls = [
        call
        for call in client.calls
        if call[0] == "POST" and call[1].endswith("/artifact-jobs")
    ]
    assert len(create_calls) == 2
    assert all(call[3] == {"X-Request-ID": CREATE_KEY} for call in create_calls)


def test_cli_interrupted_upload_preserves_draft_and_resumes_only_missing_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding(tmp_path / "job.json", {"a.png": b"a", "b.png": b"b"})
    client = ArtifactJobClient()
    client.fail_input_name = "b.png"

    first = cli.main(
        ("--json", "recipe", "job", "create", "--run", RUN_ID, "--file", str(binding)),
        control_client=client,
        request_id_factory=lambda: CREATE_KEY,
    )
    error = json.loads(capsys.readouterr().out)

    assert first == 2
    assert error["artifact_job_id"] == JOB_ID
    assert error["uploaded_inputs"] == [
        {"name": "a.png", "sha256": hashlib.sha256(b"a").hexdigest()}
    ]
    create_count = sum(
        call[0] == "POST" and call[1].endswith("/artifact-jobs")
        for call in client.calls
    )

    resumed = cli.main(
        ("--json", "recipe", "job", "upload", JOB_ID, "--file", str(binding)),
        control_client=client,
    )
    resumed_output = capsys.readouterr().out

    assert resumed == 0
    assert json.loads(resumed_output)["state"] == "ready"
    assert client.upload_calls == ["a.png", "b.png", "b.png"]
    assert (
        sum(
            call[0] == "POST" and call[1].endswith("/artifact-jobs")
            for call in client.calls
        )
        == create_count
    )


def test_cli_capability_limit_and_changed_local_input_refuse_before_byte_transfer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    too_large = _binding(tmp_path / "large.json", {"input.png": b"xx"})
    limited = ArtifactJobClient()
    limited.capabilities = _capabilities(maximum_input_file_bytes=1)

    status = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "create",
            "--run",
            RUN_ID,
            "--file",
            str(too_large),
        ),
        control_client=limited,
    )

    assert status == 2
    assert limited.job is None
    assert not limited.upload_calls
    assert "Controller limit is 1" in json.loads(capsys.readouterr().out)["error"]

    binding = _binding(tmp_path / "changed.json", {"input.png": b"a"})
    original = {"input.png": b"a"}
    client = ArtifactJobClient()
    client.job = _job(declarations=_declarations(original))
    (tmp_path / "input.png").write_bytes(b"b")

    status = cli.main(
        ("--json", "recipe", "job", "upload", JOB_ID, "--file", str(binding)),
        control_client=client,
    )

    assert status == 2
    assert not client.upload_calls
    assert "do not match" in json.loads(capsys.readouterr().out)["error"]


def test_cli_rejects_a_nonregular_input_without_opening_a_second_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding(tmp_path / "fifo.json", {"input.png": b"x"})
    input_path = tmp_path / "input.png"
    input_path.unlink()
    input_path.parent.mkdir(exist_ok=True)
    os.mkfifo(input_path)
    client = ArtifactJobClient()

    status = cli.main(
        ("--json", "recipe", "job", "create", "--run", RUN_ID, "--file", str(binding)),
        control_client=client,
    )

    assert status == 2
    assert client.job is None
    assert not client.upload_calls
    assert "regular non-symlink file" in json.loads(capsys.readouterr().out)["error"]


def test_cli_submit_and_remote_cancel_reconcile_with_the_same_caller_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ArtifactJobClient()
    client.job = _job(state="ready")
    client.lose_submit_response = True

    submitted = cli.main(
        ("--json", "recipe", "job", "submit", JOB_ID),
        control_client=client,
        request_id_factory=lambda: SUBMIT_KEY,
    )
    submit_document = json.loads(capsys.readouterr().out)

    assert submitted == 0
    assert submit_document["state"] == "queued"
    assert submit_document["submit_request_id"] == SUBMIT_KEY
    submit_posts = [
        call
        for call in client.calls
        if call[0] == "POST" and call[1].endswith("/submit")
    ]
    assert len(submit_posts) == 1
    assert submit_posts[0][3] == {"X-Request-ID": SUBMIT_KEY}

    client.job["state"] = "queued"
    client.lose_cancel_response = True
    cancelled = cli.main(
        ("--json", "recipe", "job", "cancel", JOB_ID, "--yes", "--reason", "stop now"),
        control_client=client,
        request_id_factory=lambda: CANCEL_KEY,
    )
    cancel_document = json.loads(capsys.readouterr().out)

    assert cancelled == 0
    assert cancel_document["state"] == "cancelling"
    cancel_post = next(
        call
        for call in client.calls
        if call[0] == "POST" and call[1].endswith("/cancel")
    )
    assert cancel_post[3] == {"X-Request-ID": CANCEL_KEY}
    assert cancel_post[2] == {"reason": "stop now"}
    assert cancel_document["result_evidence"]["cancel_request_id"] == CANCEL_KEY


def test_cli_submit_rejects_a_receipt_owned_by_another_request_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ArtifactJobClient()
    client.job = _job(state="queued")
    client.job["submit_request_id"] = SUBMIT_KEY
    client.ignore_submit_key_mismatch = True
    replacement_key = "00000000-0000-4000-8000-000000000007"

    status = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "submit",
            JOB_ID,
            "--request-key",
            replacement_key,
        ),
        control_client=client,
        request_id_factory=lambda: replacement_key,
    )

    result = json.loads(capsys.readouterr().out)
    assert status == 2
    assert "another submit request" in result["error"]


def test_cli_job_list_rejects_a_job_owned_by_another_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ArtifactJobClient()
    client.job = _job(state="ready")
    client.job["run_id"] = "00000000-0000-4000-8000-000000000099"

    status = cli.main(
        ("--json", "recipe", "job", "list", "--run", RUN_ID),
        control_client=client,
    )

    result = json.loads(capsys.readouterr().out)
    assert status == 2
    assert "another run" in result["error"]


def test_cli_lists_follows_and_downloads_only_manifest_named_verified_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    client = ArtifactJobClient()
    client.job = _succeeded_job()

    listed = cli.main(
        ("--json", "recipe", "job", "list", "--run", RUN_ID),
        control_client=client,
    )
    list_document = json.loads(capsys.readouterr().out)
    followed = cli.main(
        ("--json", "recipe", "job", "detail", JOB_ID, "--follow"),
        control_client=client,
    )
    detail_document = json.loads(capsys.readouterr().out)
    downloaded = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    download_document = json.loads(capsys.readouterr().out)

    assert listed == followed == downloaded == 0
    assert list_document["jobs"][0]["id"] == JOB_ID
    assert detail_document["state"] == "succeeded"
    assert download_document["files"][0]["state"] == "downloaded"
    assert (output_directory / "result.png").read_bytes() == b"x"
    assert len(client.download_calls) == 1
    assert client.download_calls[0] == (
        f"/api/artifact-jobs/{JOB_ID}/results/result.png/"
        f"{hashlib.sha256(b'x').hexdigest()}"
    )

    reused = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    reused_document = json.loads(capsys.readouterr().out)
    assert reused == 0
    assert reused_document["files"][0]["state"] == "reused"
    assert len(client.download_calls) == 1


def test_cli_checks_output_filename_aliases_on_the_destination_before_download(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    probe = output_directory / ".CaseProbe"
    probe.write_bytes(b"probe")
    case_insensitive = (output_directory / ".CASEPROBE").exists()
    probe.unlink()
    digest = hashlib.sha256(b"x").hexdigest()
    client = ArtifactJobClient()
    client.job = _succeeded_job(
        [
            {
                "name": "A.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": digest,
            },
            {
                "name": "a.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": digest,
            },
        ],
        output_limits={
            "max_files": 2,
            "max_file_bytes": 1,
            "max_total_bytes": 2,
            "allowed_media_types": ["image/png"],
        },
    )

    status = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    output = capsys.readouterr().out

    if case_insensitive:
        result = json.loads(output)
        assert status == 2
        assert "alias on this filesystem" in result["error"]
        assert client.download_calls == []
        assert list(output_directory.iterdir()) == []
    else:
        result = json.loads(output)
        assert status == 0
        assert [item["name"] for item in result["files"]] == ["A.png", "a.png"]
        assert result["files"][0]["path"] != result["files"][1]["path"]
        assert client.download_calls == [
            f"/api/artifact-jobs/{JOB_ID}/results/A.png/{digest}",
            f"/api/artifact-jobs/{JOB_ID}/results/a.png/{digest}",
        ]
        assert (output_directory / "A.png").read_bytes() == b"x"
        assert (output_directory / "a.png").read_bytes() == b"x"


def test_cli_rejects_unsafe_result_names_and_preserves_partial_downloads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    client = ArtifactJobClient()
    malicious = _succeeded_job(
        [
            {
                "name": "../escape.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        ]
    )
    client.job = malicious

    rejected = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    rejection = json.loads(capsys.readouterr().out)
    assert rejected == 2
    assert not client.download_calls
    assert not (tmp_path / "escape.png").exists()
    assert "canonical ArtifactJobResponse" in rejection["error"]

    client.job = _succeeded_job(
        [
            {
                "name": "manifest.json",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        ]
    )
    reserved_name = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    reserved_document = json.loads(capsys.readouterr().out)
    assert reserved_name == 2
    assert "file metadata is invalid" in reserved_document["error"]
    assert not client.download_calls

    media = {
        "max_files": 2,
        "max_file_bytes": 1,
        "max_total_bytes": 2,
        "allowed_media_types": ["image/png"],
    }
    first_digest = hashlib.sha256(b"x").hexdigest()
    client.job = _succeeded_job(
        [
            {
                "name": "a.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": first_digest,
            },
            {
                "name": "b.png",
                "media_type": "image/png",
                "size_bytes": 1,
                "sha256": first_digest,
            },
        ],
        output_limits=media,
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"safe")
    (output_directory / "b.png").symlink_to(outside)

    partial = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    partial_document = json.loads(capsys.readouterr().out)

    assert partial == 2
    assert partial_document["partial_collection"] is True
    assert partial_document["downloaded_file_count"] == 1
    assert partial_document["expected_file_count"] == 2
    assert (output_directory / "a.png").read_bytes() == b"x"
    assert outside.read_bytes() == b"safe"


def test_cli_refuses_to_replace_an_existing_result_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    destination = output_directory / "result.png"
    destination.write_bytes(b"old")
    client = ArtifactJobClient()
    client.job = _succeeded_job()

    status = cli.main(
        (
            "--json",
            "recipe",
            "job",
            "download",
            JOB_ID,
            "--output",
            str(output_directory),
        ),
        control_client=client,
    )
    document = json.loads(capsys.readouterr().out)

    assert status == 2
    assert "does not match the result manifest" in document["error"]
    assert destination.read_bytes() == b"old"
    assert not client.download_calls
