from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    MAX_DOCUMENT_BYTES,
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import AgentCertificate, AgentNode, Base, Job

from ..recipe_stop_fixtures import recipe_stop_payload
from ..runtime_identity_support import claim_agent

ROOT = Path(__file__).resolve().parents[3]
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
STOP_PAYLOAD = recipe_stop_payload(NODE_A, plan_digest=COMMIT)
STOP_RESULT = RecipeStopResult()
PROTOCOL_WHEEL = ROOT / "inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl"
PROTOCOL_WHEEL_HASH = hashlib.sha256(PROTOCOL_WHEEL.read_bytes()).hexdigest()
PUBLIC_CONTRACTS_WHEEL = (
    ROOT / "inventory/wheels/vonk_forge_public_contracts-2.2.0-py3-none-any.whl"
)
PUBLIC_CONTRACTS_WHEEL_HASH = hashlib.sha256(
    PUBLIC_CONTRACTS_WHEEL.read_bytes()
).hexdigest()


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
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    workload_intent_ordinal=1,
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
    return AgentJobService(sessions, clock=clock), sessions, clock


def enqueue(service: AgentJobService, sessions, clock) -> None:
    payload = {"workload_intent_ordinal": 1}
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)
    service.enqueue(parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)


def raw_stop_claim(payload: dict[str, object]) -> dict[str, object]:
    return {
        "fence": "00000000-0000-4000-8000-000000000003",
        "operation": "recipe.stop",
        "payload": payload,
        "deadline": "2026-08-03T12:00:00+00:00",
    }


def raw_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "fence": "00000000-0000-4000-8000-000000000003",
        "state": "succeeded",
        "result": result,
    }


def test_cross_node_claim_is_denied(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)

    assert claim_agent(jobs, NODE_B, "serial-b") is None


def test_revoked_certificate_cannot_publish_result(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(claim, STOP_RESULT)


def test_secret_bearing_payload_is_rejected(service) -> None:
    _jobs, sessions, clock = service
    payload = {"workload_intent_ordinal": 1}
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    with pytest.raises(AgentProtocolError, match="unsafe"):
        AgentClaim.parse(raw_stop_claim(STOP_PAYLOAD | {"private_key": "unsafe"}))


def test_payload_and_result_documents_are_size_limited(service) -> None:
    jobs, sessions, clock = service
    payload = {"workload_intent_ordinal": 1}
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    oversized_claim = raw_stop_claim({"value": "x" * (MAX_DOCUMENT_BYTES + 1)})
    oversized_claim["operation"] = "arbitrary.command"
    with pytest.raises(AgentProtocolError, match="large"):
        AgentClaim.parse(oversized_claim)

    jobs.enqueue(parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with pytest.raises(AgentProtocolError, match="large"):
        AgentResult.parse(raw_result({"value": "x" * (MAX_DOCUMENT_BYTES + 1)}))


def test_stale_fence_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    clock.advance(31)
    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, STOP_RESULT)


def test_protocol_has_no_arbitrary_operation_member() -> None:
    assert AgentOperation.ARTIFACT_DISTRIBUTION.value == "artifact.distribution.v1"
    with pytest.raises(ValueError):
        AgentOperation("arbitrary.command")


def test_release_artifacts_install_the_exact_protocol_wheel() -> None:
    control_project = (ROOT / "control/pyproject.toml").read_text()
    protocol_wheel_path = PROTOCOL_WHEEL
    contracts_wheel_path = PUBLIC_CONTRACTS_WHEEL
    packaging_lock = (ROOT / "control/packaging/public-contracts.lock").read_text()
    packaging_source = tomllib.loads(packaging_lock)
    dockerignore_path = ROOT / ".dockerignore"
    dockerfile = (ROOT / "control/Dockerfile").read_text()

    assert protocol_wheel_path.is_file()
    assert contracts_wheel_path.is_file()
    assert dockerignore_path.is_file()
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text())
    protocol_sources = [
        package["source"]
        for package in control_lock["package"]
        if package["name"] == "vonk-agent-protocol"
    ]
    contract_package = next(
        package
        for package in control_lock["package"]
        if package["name"] == "vonk-forge-public-contracts"
    )

    assert '"vonk-agent-protocol==4.1.0"' in control_project
    assert protocol_sources == [
        {"path": "../inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl"}
    ]
    assert control_lock["package"][
        next(
            index
            for index, package in enumerate(control_lock["package"])
            if package["name"] == "vonk-agent-protocol"
        )
    ]["wheels"] == [
        {
            "filename": "vonk_agent_protocol-4.1.0-py3-none-any.whl",
            "hash": f"sha256:{PROTOCOL_WHEEL_HASH}",
        }
    ]
    revision = packaging_source["revision"]
    assert len(revision) == 40 and set(revision) <= set("0123456789abcdef")
    assert (
        packaging_source["source"]
        == "https://github.com/CarstVaartjes/vonk-forge-recipes.git"
    )
    assert packaging_source["branch"] == "main"
    assert packaging_source["subdirectory"] == "contracts"
    assert contract_package["source"] == {
        "git": f"{packaging_source['source']}?subdirectory=contracts&branch=main#{revision}"
    }
    assert "wheels" not in contract_package
    assert 'branch = "main"' in packaging_lock
    assert f'sha256 = "{PUBLIC_CONTRACTS_WHEEL_HASH}"' in packaging_lock
    assert "COPY control/pyproject.toml ./" in dockerfile
    assert "COPY control/src ./src" in dockerfile
    assert (
        "COPY inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl /wheels/"
        in dockerfile
    )
    assert "/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl" in dockerfile
    assert (
        "python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels /agent-protocol"
        not in dockerfile
    )
    assert "git clone --filter=blob:none --no-checkout" in dockerfile
    assert "git -C /public-contracts checkout --detach" in dockerfile
    assert (
        "python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels" in dockerfile
    )
    assert "/public-contracts/contracts" in dockerfile
    assert (
        "COPY inventory/wheels/vonk_forge_public_contracts-2.2.0-py3-none-any.whl /wheels/"
        not in dockerfile
    )
    dockerignore = set(dockerignore_path.read_text().splitlines())
    assert "*" in dockerignore
    lines = dockerignore_path.read_text().splitlines()
    last_include = max(
        index
        for index, line in enumerate(lines)
        if line.startswith("!") and line != "!install/installer-release-public.pem"
    )
    assert {
        "!control/src/**",
        "!control/web/**",
        "control/.venv",
        "!inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl",
        "!inventory/wheels/vonk_forge_public_contracts-2.2.0-py3-none-any.whl",
    } <= dockerignore
    assert "!agent_protocol/src/**" not in dockerignore
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
    assert {line for line in lines[last_include + 1 :] if line.startswith("!")} == {
        "!install/installer-release-public.pem"
    }


def test_control_environment_installs_the_verified_protocol_wheel() -> None:
    # The suite runs in the synced control environment, so its installed
    # distribution is the one under test.
    installed = importlib.metadata.distribution("vonk-agent-protocol")
    direct_url_text = installed.read_text("direct_url.json")
    assert direct_url_text is not None
    direct_url = json.loads(direct_url_text)
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text())
    package = next(
        package
        for package in control_lock["package"]
        if package["name"] == "vonk-agent-protocol"
    )

    assert direct_url["url"].endswith(
        "/inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl"
    )
    assert package["source"] == {
        "path": "../inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl"
    }
    assert package["wheels"] == [
        {
            "filename": "vonk_agent_protocol-4.1.0-py3-none-any.whl",
            "hash": f"sha256:{PROTOCOL_WHEEL_HASH}",
        }
    ]


def test_control_environment_preserves_the_canonical_zero_byte_model_contract() -> None:
    from vonk_agent_protocol import AgentProtocolError, DistributionObject

    empty = DistributionObject.parse(
        {
            "name": "support/empty.safetensors",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "bytes": 0,
            "kind": "model",
        }
    )
    assert empty.bytes == 0
    with pytest.raises(AgentProtocolError):
        DistributionObject.parse(
            {
                "name": "support/empty.safetensors",
                "sha256": "0" * 64,
                "bytes": 0,
                "kind": "model",
            }
        )


# Checks the Controller image the image-build CI job built once from the root
# context; it never builds one.
@pytest.mark.built_image
@pytest.mark.lane  # Runs the prebuilt root-context Controller image.
def test_root_context_image_installs_contracts_and_protocol_from_build_inputs(
    controller_image: str,
) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            controller_image,
            "-c",
            dedent(
                """
                import importlib.metadata, json
                from importlib.resources import files
                from vonk_agent_protocol import AgentProtocolError, DistributionObject
                from vonk_control.recipe_runtime_specs import compile_runtime_spec
                from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256, read_model, read_recipe

                def rejects(value):
                    try:
                        DistributionObject.parse(value)
                    except AgentProtocolError:
                        return True
                    return False

                recipe = json.loads(files("vonk_forge_contracts").joinpath("examples/recipe-source-build.json").read_text())
                model = json.loads(files("vonk_forge_contracts").joinpath("examples/model-definition.json").read_text())
                RecipeDefinition.model_validate(recipe)
                ModelDefinition.model_validate(model)
                compiled = compile_runtime_spec(read_recipe(recipe), recipe_digest=document_sha256(recipe), models={document_sha256(model): read_model(model)}, package_handle={"image_digest": "d" * 64, "image_reference": "localhost/vonk/build@sha256:" + "d" * 64, "paths": ["context.tar", "Dockerfile"]}, role="entrypoint", rank=0)
                empty = DistributionObject.parse({"name": "support/empty.safetensors", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "bytes": 0, "kind": "model"})
                inconsistent = rejects({"name": "support/empty.safetensors", "sha256": "0" * 64, "bytes": 0, "kind": "model"})
                print(json.dumps({"protocol": importlib.metadata.version("vonk-agent-protocol"), "contracts": importlib.metadata.version("vonk-forge-public-contracts"), "model": ModelDefinition.__name__, "recipe": RecipeDefinition.__name__, "distribution": DistributionObject.__name__, "zero_byte_model": empty.bytes == 0, "inconsistent_zero_byte_rejected": inconsistent, "compiled_interface": compiled.runtime.interface, "compiled_image": compiled.runtime.image}))
                """
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = json.loads(result.stdout)

    assert installed == {
        "protocol": "4.1.0",
        "contracts": "2.2.0",
        "model": "ModelDefinition",
        "recipe": "RecipeDefinition",
        "distribution": "DistributionObject",
        "zero_byte_model": True,
        "inconsistent_zero_byte_rejected": True,
        "compiled_interface": "vonk.runtime.v1",
        "compiled_image": "localhost/vonk/build@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    }


@pytest.mark.lane  # Builds a throwaway image from the root build context.
@pytest.mark.parametrize(
    "relative_path",
    [
        "install/private.pem",
        "control/src/private.pem",
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
