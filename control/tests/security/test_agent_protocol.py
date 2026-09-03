from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation, AgentProtocolError
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import AgentCertificate, AgentNode, Base, Job

from ..runtime_identity_support import claim_agent

ROOT = Path(__file__).resolve().parents[3]
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
PROBE_RESULT = {
    "status": "ok",
    "evidence": {
        "vonk_forge": {
            "schema_version": 1,
            "memory": {"available_bytes": 1_000},
            "storage": {"available_bytes": 2_000},
            "accelerator": {"available": True},
        },
        "nvidia": {"tools": {}},
    },
}
PROTOCOL_WHEEL = ROOT / "inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl"
PROTOCOL_WHEEL_HASH = hashlib.sha256(PROTOCOL_WHEEL.read_bytes()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-protocol.sqlite'}")
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    return AgentJobService(sessions, clock=clock), sessions, clock


def enqueue(service: AgentJobService, sessions, clock) -> None:
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)
    service.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {})


def test_cross_node_claim_is_denied(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)

    assert claim_agent(jobs, NODE_B, "serial-b", 30) is None


def test_revoked_certificate_cannot_publish_result(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(claim, PROBE_RESULT)


def test_secret_bearing_payload_is_rejected(service) -> None:
    jobs, sessions, clock = service
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    with pytest.raises(AgentProtocolError, match="unsafe"):
        jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {"private_key": "unsafe"})


def test_payload_and_result_documents_are_size_limited(service) -> None:
    jobs, sessions, clock = service
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    with pytest.raises(AgentProtocolError, match="large"):
        jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {"value": "x" * 65_536})

    jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {})
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None
    with pytest.raises(AgentProtocolError, match="large"):
        jobs.succeed(claim, {"value": "x" * 65_536})


def test_stale_fence_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None
    clock.advance(31)
    assert claim_agent(jobs, NODE_A, "serial-a", 30) is not None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, PROBE_RESULT)


def test_protocol_has_no_arbitrary_operation_member() -> None:
    with pytest.raises(ValueError):
        AgentOperation("arbitrary.command")


def test_release_artifacts_install_the_exact_protocol_wheel() -> None:
    control_project = (ROOT / "control/pyproject.toml").read_text()
    protocol_wheel_path = PROTOCOL_WHEEL
    dockerignore_path = ROOT / ".dockerignore"
    dockerfile = (ROOT / "control/Dockerfile").read_text()

    assert protocol_wheel_path.is_file()
    assert dockerignore_path.is_file()
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text())
    protocol_sources = [
        package["source"]
        for package in control_lock["package"]
        if package["name"] == "vonk-agent-protocol"
    ]

    assert '"vonk-agent-protocol==2.2.0"' in control_project
    assert protocol_sources == [
        {"path": "../inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl"},
    ]
    assert "COPY control/pyproject.toml ./" in dockerfile
    assert "COPY control/src ./src" in dockerfile
    assert (
        "COPY inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl /wheels/"
        in dockerfile
    )
    assert "/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl" in dockerfile
    dockerignore = set(dockerignore_path.read_text().splitlines())
    assert "*" in dockerignore
    lines = dockerignore_path.read_text().splitlines()
    last_include = max(
        index for index, line in enumerate(lines) if line.startswith("!")
    )
    assert {
        "!control/src/**",
        "!control/web/**",
        "control/.venv",
        "!inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl",
    } <= dockerignore
    assert {
        "**/__pycache__/**",
        "**/*.py[cod]",
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/.pytest_cache/**",
        "**/.coverage*",
        "**/coverage/**",
        "**/htmlcov/**",
        "**/build/**",
        "**/dist/**",
        "**/.npmrc",
        "**/.netrc",
        "**/.pypirc",
        "**/.git-credentials",
        "**/.ssh/**",
        "**/credentials.json",
        "**/credentials.yaml",
        "**/credentials.yml",
        "**/credentials.toml",
        "**/secrets.json",
        "**/secrets.yaml",
        "**/secrets.yml",
        "**/secrets.toml",
    } <= set(lines[last_include + 1 :])
    assert all(not line.startswith("!") for line in lines[last_include + 1 :])


def test_control_environment_installs_the_verified_protocol_wheel() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            "control",
            "python",
            "-c",
            "import importlib.metadata, json; d = importlib.metadata.distribution('vonk-agent-protocol'); print((d._path / 'direct_url.json').read_text())",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    direct_url = json.loads(result.stdout)
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text())
    package = next(
        package
        for package in control_lock["package"]
        if package["name"] == "vonk-agent-protocol"
    )

    assert direct_url["url"].endswith(
        "/inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl"
    )
    assert package["wheels"] == [
        {
            "filename": "vonk_agent_protocol-2.2.0-py3-none-any.whl",
            "hash": f"sha256:{PROTOCOL_WHEEL_HASH}",
        }
    ]


def test_root_context_image_installs_the_verified_protocol_wheel() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    if (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        != 0
    ):
        pytest.skip("Docker daemon is unavailable")
    image = "vonk-control:test-protocol-wheel"
    build = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            "control/Dockerfile",
            "--build-arg",
            "NODE_IMAGE=node:24-bookworm-slim@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7",
            "--build-arg",
            "PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b",
            "--tag",
            image,
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import importlib.metadata, json; d = importlib.metadata.distribution('vonk-agent-protocol'); print(json.dumps({'version': d.version, 'direct_url': json.loads((d._path / 'direct_url.json').read_text())}))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = json.loads(result.stdout)

    assert installed["version"] == "2.2.0"
    assert (
        installed["direct_url"]["url"]
        == "file:///wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl"
    )
    assert (
        installed["direct_url"]["archive_info"]["hash"]
        == f"sha256={PROTOCOL_WHEEL_HASH}"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "control/src/.npmrc",
        "control/src/.netrc",
        "control/src/credentials.json",
        "control/src/secrets.yaml",
        "control/src/.ssh/id_ed25519",
        "control/web/.pypirc",
        "control/web/.git-credentials",
        "control/web/secrets.toml",
    ],
)
def test_root_context_cannot_copy_reincluded_credential_artifacts(
    relative_path: str,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    if (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        != 0
    ):
        pytest.skip("Docker daemon is unavailable")
    artifact = ROOT / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("credential=test\n")
    try:
        result = subprocess.run(
            ["docker", "build", "--file", "-", "."],
            cwd=ROOT,
            input=f"FROM scratch\nCOPY {relative_path} /forbidden\n",
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        artifact.unlink()
        while artifact.parent != ROOT and not any(artifact.parent.iterdir()):
            artifact = artifact.parent
            artifact.rmdir()

    assert result.returncode != 0
