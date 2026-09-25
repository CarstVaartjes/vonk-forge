"""The installed CLI reconnects to the same profile owner after API death."""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import Base, FleetProfileApplication

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profiles import _SwitchAdapter
from .test_fleet_profiles_canonical import NODE_1, NOW, _seed
from .test_profile_load_installed_cli import _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def _certificate(tmp_path: Path) -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = tmp_path / "controller-process-certificate.pem"
    private_key_path = tmp_path / "controller-process-private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o600)
    return certificate_path, private_key_path


def _server_process(
    *,
    tmp_path: Path,
    listener: socket.socket,
    certificate: Path,
    private_key: Path,
    trace: Path,
    database_url: str,
) -> tuple[subprocess.Popen[str], Path]:
    ready = tmp_path / f"api-ready-{uuid.uuid4().hex}"
    helper = Path(__file__).with_name("_profile_https_process.py")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(path)
            for path in (
                Path(__file__).resolve().parents[1],
                Path(__file__).resolve().parents[1] / "src",
                Path(__file__).resolve().parents[2] / "src",
                Path("/opt/vonk-forge-recipes/contracts/src"),
            )
        ),
        "VONK_TEST_DATABASE_URL": database_url,
        "VONK_RECIPE_LIBRARY_ROOT": "/opt/vonk-forge-recipes",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            "--socket-fd",
            str(listener.fileno()),
            "--ready",
            str(ready),
            "--trace",
            str(trace),
            "--certificate",
            str(certificate),
            "--private-key",
            str(private_key),
        ],
        env=environment,
        cwd=tmp_path,
        pass_fds=(listener.fileno(),),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not ready.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"Controller process exited before HTTPS startup: {stdout}{stderr}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"Controller process did not become ready: {stdout}{stderr}"
            )
        time.sleep(0.02)
    return process, ready


def _trace(trace_path: Path) -> list[dict[str, object]]:
    if not trace_path.exists():
        return []
    return [json.loads(line) for line in trace_path.read_text().splitlines()]


def _wait_for_trace(
    trace_path: Path,
    *,
    process_id: int,
    method: str,
    path: str,
    cli_process: subprocess.Popen[str],
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        entries = _trace(trace_path)
        if any(
            entry.get("pid") == process_id
            and entry.get("method") == method
            and entry.get("path") == path
            for entry in entries
        ):
            return
        if cli_process.poll() is not None:
            stdout, stderr = cli_process.communicate(timeout=5)
            raise AssertionError(
                f"installed CLI exited before expected request: {stdout}{stderr}"
            )
        time.sleep(0.02)
    raise AssertionError(
        f"Controller process {process_id} did not serve {method} {path}"
    )


@pytest.mark.lane
def test_installed_cli_follows_same_profile_application_after_api_process_restart(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    seed_api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    created = seed_api.put(
        "/api/profile/1",
        headers=headers,
        json={
            "name": "Process restart pending",
            "expected_revision": 0,
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/synthetic-tiny-image",
                    "spark_ids": [NODE_1],
                    "desired_state": "running",
                    "assignment_name": "restart-owner",
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    preview_response = seed_api.post("/api/profile/1/preview", headers=headers)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["allowed"] is True
    assert preview["steps"]
    seed_api.close()
    plan_digest = preview.get("plan_digest")
    assert isinstance(plan_digest, str)
    request_key = str(uuid.uuid4())
    certificate, private_key = _certificate(tmp_path)
    trace_path = tmp_path / "controller-requests.jsonl"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    listener.set_inheritable(True)
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    profile_number = 1
    load_path = f"/api/profile/{profile_number}/load"
    first_server: subprocess.Popen[str] | None = None
    second_server: subprocess.Popen[str] | None = None
    cli_process: subprocess.Popen[str] | None = None
    try:
        first_server, first_ready = _server_process(
            tmp_path=tmp_path,
            listener=listener,
            certificate=certificate,
            private_key=private_key,
            trace=trace_path,
            database_url=database_url,
        )
        first_pid = int(first_ready.read_text())
        environment = _process_environment(tmp_path, url, certificate, headers)
        command = [
            str(installed_vonkctl),
            "--profile",
            str(profile_number),
            "profile",
            "load",
            "--expected-plan",
            plan_digest,
            "--yes",
            "--request-key",
            request_key,
            "--timeout-seconds",
            "60",
            "--interval-seconds",
            "0.1",
            "--json",
        ]
        cli_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=tmp_path,
            text=True,
        )
        application_path: str | None = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sessions() as session:
                application = session.scalar(
                    select(FleetProfileApplication).where(
                        FleetProfileApplication.request_key == request_key
                    )
                )
                if application is not None:
                    application_id = application.id
                    application_state = application.state
                    profile_digest = application.profile_digest
                    accepted_plan_digest = application.plan_digest
                    accepted_created_at = application.created_at
                    accepted_updated_at = application.updated_at
                    accepted_progress = deepcopy(application.progress)
                    accepted_current_step = application.current_step
                    accepted_operation_id = application.current_operation_id
                    accepted_result = deepcopy(application.result)
                    application_path = f"/api/profile/applications/{application_id}"
                    break
            if cli_process.poll() is not None:
                stdout, stderr = cli_process.communicate(timeout=5)
                raise AssertionError(
                    f"installed CLI did not leave an accepted owner: {stdout}{stderr}"
                )
            time.sleep(0.02)
        else:
            raise AssertionError("profile application was not durably accepted")

        assert application_state == "queued"
        intended = accepted_progress.get("intended_profile")
        assert isinstance(intended, dict)
        assert intended.get("reviewed_plan_digest") == plan_digest
        assert isinstance(accepted_plan_digest, str) and accepted_plan_digest
        assert profile_digest
        assert application_path is not None
        _wait_for_trace(
            trace_path,
            process_id=first_pid,
            method="GET",
            path=application_path,
            cli_process=cli_process,
        )

        first_server.kill()
        assert first_server.wait(timeout=5) == -signal.SIGKILL
        second_server, second_ready = _server_process(
            tmp_path=tmp_path,
            listener=listener,
            certificate=certificate,
            private_key=private_key,
            trace=trace_path,
            database_url=database_url,
        )
        second_pid = int(second_ready.read_text())
        assert second_pid != first_pid
        _wait_for_trace(
            trace_path,
            process_id=second_pid,
            method="GET",
            path=application_path,
            cli_process=cli_process,
        )

        # Reconnection is proved above. Stop observation locally rather than
        # race a short deadline against the fresh server's startup budget.
        cli_process.send_signal(signal.SIGINT)
        stdout, stderr = cli_process.communicate(timeout=15)
        assert cli_process.returncode == 130, stdout + stderr
        assert stderr == ""
        assert stdout.count("\n") == 1
        observed = json.loads(stdout)
        assert observed["observation"]["status"] == "interrupted"
        assert observed["result"]["id"] == application_id
        assert observed["result"]["request_key"] == request_key
        token = headers["Authorization"].removeprefix("Bearer ")
        assert token not in stdout + stderr

        reconnected = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                str(profile_number),
                "profile",
                "progress",
                "--request-key",
                request_key,
                "--json",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            cwd=tmp_path,
            text=True,
            timeout=15,
            check=False,
        )
        assert reconnected.returncode == 0, reconnected.stdout + reconnected.stderr
        assert reconnected.stdout.count("\n") == 1
        progress = json.loads(reconnected.stdout)
        assert progress["id"] == application_id
        assert progress["request_key"] == request_key
        assert progress["state"] == application_state

        with sessions() as session:
            owners = list(session.scalars(select(FleetProfileApplication)))
            owner = session.get(FleetProfileApplication, application_id)
            assert owner is not None
            assert len(owners) == 1
            assert owner.id == application_id
            assert owner.request_key == request_key
            assert owner.state == application_state
            assert owner.profile_digest == profile_digest
            assert owner.plan_digest == accepted_plan_digest
            assert owner.created_at == accepted_created_at
            assert owner.updated_at == accepted_updated_at
            assert owner.progress["intended_profile"] == accepted_progress["intended_profile"]
            assert owner.progress["operation_kind"] == accepted_progress["operation_kind"]
            assert owner.progress["retry_of_application_id"] == accepted_progress[
                "retry_of_application_id"
            ]
            assert owner.progress["total_steps"] == accepted_progress["total_steps"]
            assert owner.progress["admission_pending"] is False
            assert owner.progress["admission_retry_at"] is None
            assert owner.progress["workload_intent_ordinal"] is not None
            completed_steps = owner.progress["completed_steps"]
            accepted_completed_steps = accepted_progress["completed_steps"]
            assert isinstance(completed_steps, int)
            assert isinstance(accepted_completed_steps, int)
            assert completed_steps >= accepted_completed_steps
            assert owner.current_step == accepted_current_step
            assert owner.current_operation_id == accepted_operation_id
            assert owner.result == accepted_result
            assert owner.progress.get("cancellation") is None

        entries = _trace(trace_path)
        assert (
            sum(
                entry.get("method") == "POST" and entry.get("path") == load_path
                for entry in entries
            )
            == 1
        )
        assert not any(
            entry.get("method") == "POST"
            and str(entry.get("path", "")).endswith("/cancel")
            for entry in entries
        )
        assert any(
            entry.get("pid") == second_pid
            and entry.get("method") == "GET"
            and entry.get("path")
            == f"/api/profile/{profile_number}/requests/{request_key}"
            for entry in entries
        )
    finally:
        if cli_process is not None and cli_process.poll() is None:
            cli_process.kill()
            cli_process.communicate(timeout=5)
        for process in (second_server, first_server):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        listener.close()
