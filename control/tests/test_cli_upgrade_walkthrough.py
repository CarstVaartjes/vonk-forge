"""Disposable installed-CLI U8 upgrade setup; cleanup remains gated separately."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Base,
    Job,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.operator_projection_api import FleetOperatorServices

from .test_agent_upgrades import NODE_A, NODE_B, OLD_IDENTITY, PACKAGE, REVISION, SOURCE
from .test_cli_first_connection_endpoints_installed import _Authority
from .test_cli_operator_walkthrough import (
    _AuthorizationHeaders,
    _session_environment,
)
from .test_profile_load_installed_cli import (
    _build_installed_vonkctl,
    _https_api_peer,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_U8_UPGRADE_MODE" not in os.environ,
        reason="set VONK_U8_UPGRADE_MODE=smoke or interactive to opt in",
    ),
]

_FIXTURE_FAILURE = "agent upgrade request is invalid"


def _registered_upgrade_api(
    engine: Engine,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    sessionmaker[Session],
    AgentJobService,
    TestClient,
    dict[str, str],
]:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=8),
                    fingerprint=f"fingerprint-{serial}",
                )
            )

    # The disposable source is shape-validated, while external release
    # publication and package-signature verification remain outside this lane.
    monkeypatch.setattr(
        "vonk_control.agent_upgrades.load_package_source",
        lambda *_: AgentPackageSource.model_validate(SOURCE),
    )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )
    monkeypatch.setattr(upgrades, "current_package", lambda: dict(PACKAGE))
    operations.set_result_consumer(upgrades.consume_agent_result)

    tokens = TokenCodec(os.urandom(32))
    projected = durable_operation_services(
        sessions,
        workspace / "route-storage",
        clock=lambda: now,
        cursors=tokens.cursor_codec(),
        resume_agent_upgrade=upgrades.resume,
    )
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=tokens,
        audits=MemoryAuditStore(),
        fleet_projection=FleetProjection(_Authority(), sessions, clock=lambda: now),
        fleet_services=FleetOperatorServices(upgrades=upgrades),
        operations=projected,
        now=lambda: 100,
    )
    headers = {
        "Authorization": "Bearer "
        + tokens.issue(Actor("u8-facilitator", "administrator"), ttl_seconds=200, now=0)
    }
    return sessions, operations, TestClient(app), _AuthorizationHeaders(headers)


def _cli(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(executable), *arguments],
        stdin=subprocess.DEVNULL,
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    token = Path(environment["VONK_CONTROL_TOKEN_FILE"]).read_text(encoding="utf-8")
    if token in result.stdout or token in result.stderr:
        pytest.fail("installed CLI exposed the private U8 credential", pytrace=False)
    return result


def _json_line(output: str) -> dict[str, object]:
    assert output.endswith("\n") and output.count("\n") == 1
    value = json.loads(output)
    assert isinstance(value, dict)
    return value


def _fail_first_target(operations: AgentJobService) -> None:
    first = operations.claim(
        NODE_A,
        "serial-a",
        30,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity=OLD_IDENTITY,
    )
    assert first is not None
    operations.fail(first, _FIXTURE_FAILURE)


def _assert_first_failure_stops_rollout(
    sessions: sessionmaker[Session],
    operations: AgentJobService,
    request_key: str,
    job_id: str,
) -> None:
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
            )
        )
        assert len(jobs) == 1
        assert jobs[0].id == job_id and jobs[0].state == "waiting-for-operator"
        assert len(children) == 1
        assert children[0].node_id == NODE_A
        assert children[0].state == "waiting-for-operator"
    assert (
        operations.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )


def _smoke(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    sessions: sessionmaker[Session],
    operations: AgentJobService,
    peer_calls: list[tuple[str, str, object]],
) -> None:
    refused = _cli(
        executable,
        environment,
        cwd,
        "--no-input",
        "fleet",
        "upgrade",
        "--all",
        "--strategy",
        "one-at-a-time",
        "--json",
    )
    refusal = _json_line(refused.stdout)
    refusal_error = refusal.get("error")
    assert refused.returncode == 2
    assert isinstance(refusal_error, str) and "--yes" in refusal_error
    assert refused.stderr == ""
    assert peer_calls == []
    with sessions() as session:
        assert not tuple(session.scalars(select(Job)))

    request_key = str(uuid4())
    accepted = _cli(
        executable,
        environment,
        cwd,
        "--no-input",
        "fleet",
        "upgrade",
        "--all",
        "--yes",
        "--strategy",
        "one-at-a-time",
        "--request-key",
        request_key,
        "--detach",
        "--json",
    )
    receipt = _json_line(accepted.stdout)
    operation_id = receipt.get("operation_id")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert accepted.stderr == ""
    assert receipt["request_key"] == request_key
    assert isinstance(operation_id, str)
    job_id = operation_id
    assert receipt["targets"] == [NODE_A, NODE_B]
    assert peer_calls == [
        (
            "POST",
            "/api/fleet/upgrade",
            {
                "all": True,
                "request_key": request_key,
                "strategy": "one-at-a-time",
            },
        )
    ]

    _fail_first_target(operations)
    progress = _cli(executable, environment, cwd, "fleet", "progress", job_id, "--json")
    result = _json_line(progress.stdout)
    assert progress.returncode == 0, progress.stdout + progress.stderr
    assert result["id"] == job_id
    assert result["state"] == "waiting-for-operator"
    _assert_first_failure_stops_rollout(sessions, operations, request_key, job_id)
    print(
        "U8 upgrade smoke passed: installed CLI refused an unconsented redirected "
        "request before API dispatch, then returned one JSON acceptance receipt; "
        "the registered PostgreSQL owner stopped after the first fixture failure. "
        "No external package publisher or physical Spark was exercised.",
        flush=True,
    )


class _FirstFailureExecutor:
    """Model one deterministic agent using the actual PostgreSQL claim owner."""

    def __init__(self, operations: AgentJobService) -> None:
        self._operations = operations
        self._stop = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise TimeoutError("disposable U8 executor did not stop")
        if self.error is not None:
            raise RuntimeError("disposable U8 executor failed") from self.error

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                claim = self._operations.claim(
                    NODE_A,
                    "serial-a",
                    30,
                    capabilities=[
                        "agent.runtime.rust.v1",
                        "agent.upgrade.v1",
                    ],
                    runtime_identity=OLD_IDENTITY,
                )
                if claim is not None:
                    self._operations.fail(claim, _FIXTURE_FAILURE)
                    self.finished.set()
                    return
                self._stop.wait(0.2)
        except Exception as error:  # noqa: BLE001 - surface worker-thread failures.
            self.error = error
            self.finished.set()


def _walkthrough(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    workspace_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="vonk-cli-u8-upgrade-") as temporary:
        workspace = Path(temporary)
        workspace_path = workspace
        workspace.chmod(0o700)
        operator_cwd = workspace / "operator-cwd"
        operator_cwd.mkdir(mode=0o700)
        sessions, operations, api, headers = _registered_upgrade_api(
            engine, workspace, monkeypatch
        )
        executable = _build_installed_vonkctl(workspace / "installed-cli")
        with (
            api,
            _https_api_peer(workspace, api, headers) as (
                url,
                certificate,
                peer,
            ),
        ):
            environment = _session_environment(
                installed_vonkctl=executable,
                workspace=workspace,
                url=url,
                certificate=certificate,
                headers=headers,
            )
            token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            token = headers["Authorization"].removeprefix("Bearer ")
            assert token_file.read_text(encoding="utf-8") == token
            if mode == "smoke":
                _smoke(
                    executable=executable,
                    environment=environment,
                    cwd=operator_cwd,
                    sessions=sessions,
                    operations=operations,
                    peer_calls=peer.calls,
                )
            else:
                executor = _FirstFailureExecutor(operations)
                executor.start()
                try:
                    print(f"Disposable Controller: {url}", flush=True)
                    print(f"Installed CLI: {executable}", flush=True)
                    print(
                        "The original U8 outcome card and shipped runbook remain "
                        "the participant materials. This private fixture has two "
                        "test nodes and no physical Spark target. Cleanup is not "
                        "available in this session; its W09/W17 gate remains open. "
                        "No token value is shown. Close the shell with `exit` or "
                        "Ctrl-D to remove this run's files and state.",
                        flush=True,
                    )
                    shell = shutil.which("bash") or "/bin/bash"
                    completed = subprocess.run(
                        [shell, "--noprofile", "--norc", "-i"],
                        env=environment,
                        cwd=operator_cwd,
                        check=False,
                    )
                    print(
                        f"Operator shell exited with status {completed.returncode}.",
                        flush=True,
                    )
                    with sessions() as session:
                        accepted_jobs = tuple(
                            session.scalars(
                                select(Job).where(Job.kind == "agent-upgrade")
                            )
                        )
                    if accepted_jobs:
                        assert len(accepted_jobs) == 1
                        job = accepted_jobs[0]
                        if (
                            not executor.finished.is_set()
                            and not executor.finished.wait(30)
                        ):
                            raise TimeoutError(
                                "accepted U8 upgrade did not reach the first fixture"
                            )
                        if executor.error is not None:
                            raise RuntimeError(
                                "disposable U8 executor failed"
                            ) from executor.error
                        _assert_first_failure_stops_rollout(
                            sessions,
                            operations,
                            job.request_id,
                            job.id,
                        )
                finally:
                    executor.close()
    assert workspace_path is not None and not workspace_path.exists()
    print(
        "U8 temporary installed wheel, HTTPS credentials, private HOME, and "
        "route files were removed; shell history writing was disabled. Pytest "
        "teardown drops this run's PostgreSQL database and stops its disposable "
        "PostgreSQL container.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_U8_UPGRADE_MODE") != "smoke",
    reason="set VONK_U8_UPGRADE_MODE=smoke to run U8 upgrade smoke",
)
def test_disposable_u8_upgrade_walkthrough_smoke(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _walkthrough(postgres_engine, monkeypatch, "smoke")


@pytest.mark.skipif(
    os.environ.get("VONK_U8_UPGRADE_MODE") != "interactive",
    reason="set VONK_U8_UPGRADE_MODE=interactive to start U8 shell",
)
def test_disposable_u8_upgrade_walkthrough_interactive(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _walkthrough(postgres_engine, monkeypatch, "interactive")
