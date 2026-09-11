from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    RestartVonkUnitOperation,
    SignedHostHelperGrant,
    SignedRecipeRunObservationReceipt,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_control.host_helper_authority import HostHelperGrantIssuer

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system

FIXTURES = Path(__file__).parents[2] / "agent_protocol" / "fixtures"
PRIVATE_SEED = bytes([19]) * 32
PUBLIC_KEY = bytes.fromhex(
    "66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633"
)


@pytest.fixture(scope="session")
def host_helper_wire_probe() -> Path:
    configured = os.environ.get("VONK_HOST_HELPER_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(
                f"configured host helper wire probe is not executable: {path}"
            )
        return path

    repository = Path(__file__).resolve().parents[2]
    target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
    if not target_root.is_absolute():
        target_root = repository / target_root
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--package",
            "vonk-agent-helper",
            "--example",
            "host_helper_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    target = target_root / "debug" / "examples" / "host_helper_wire_probe"
    if not target.is_file() or not os.access(target, os.X_OK):
        raise AssertionError(
            f"cargo did not produce an executable wire probe: {target}"
        )
    return target


def test_python_issuer_matches_the_rust_verified_host_grant_fixture() -> None:
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED),
        clock=lambda: datetime.fromtimestamp(2_100_000_000, UTC),
        request_id_factory=lambda: "10000000-0000-4000-8000-000000000001",
    )
    grant = issuer.issue_grant(
        node_id="spk_11111111111111111111111111111111",
        operation=RestartVonkUnitOperation(type="restart-vonk-unit", unit="agent"),
        expires_in_seconds=60,
    )
    raw = (FIXTURES / "host-helper-grant-python-issued.json").read_bytes().rstrip(b"\n")

    assert canonical_message(grant.to_mapping()) == raw
    parsed = SignedHostHelperGrant.parse(json.loads(raw))
    ed25519.Ed25519PublicKey.from_public_bytes(PUBLIC_KEY).verify(
        bytes.fromhex(parsed.signature.value),
        b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
        + canonical_message(parsed.claims.to_mapping()),
    )


def test_rust_signed_receipt_fixture_is_a_controller_verifiable_wire_message() -> None:
    raw = (FIXTURES / "recipe-run-observation-receipt.json").read_bytes().rstrip(b"\n")
    receipt = SignedRecipeRunObservationReceipt.parse(json.loads(raw))

    assert canonical_message(receipt.to_mapping()) == raw
    assert receipt.claims.request_id == "10000000-0000-4000-8000-000000000001"
    ed25519.Ed25519PublicKey.from_public_bytes(PUBLIC_KEY).verify(
        bytes.fromhex(receipt.signature.value),
        recipe_run_observation_receipt_signing_bytes(receipt.claims),
    )


def test_api_grant_crosses_rust_helper_and_python_controller_wire_boundary(
    agent_system, host_helper_wire_probe: Path
) -> None:
    client, services, _, _clock = agent_system
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED),
        clock=lambda: datetime.fromtimestamp(2_100_000_000, UTC),
        request_id_factory=lambda: "10000000-0000-4000-8000-000000000001",
    )

    class RecordingHostAuthority:
        public_key_document = issuer.public_key_document()

        def issue_grant(
            self,
            *,
            node_id: str,
            job_id: str,
            operation_id: str,
            attempt: int,
            fence: str,
            action: ContainerRuntimeAction,
            request_sha256: str,
            certificate_serial: str,
            installation_id: str | None = None,
            expires_in_seconds: int = 30,
        ) -> SignedHostHelperGrant:
            return issuer.issue_grant(
                node_id=node_id,
                operation=ExecuteContainerRuntimeRequestOperation(
                    type="execute-container-runtime-request",
                    action=action.value,
                    job_id=job_id,
                    operation_id=operation_id,
                    attempt=attempt,
                    fence=fence,
                    request_sha256=request_sha256,
                    observation_identity_sha256="b" * 64,
                ),
                expires_in_seconds=expires_in_seconds,
            )

    object.__setattr__(services, "host_runtime_authority", RecordingHostAuthority())
    request = {
        "node_id": NODE_A,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "action": "run-inspect",
        "request_sha256": "a" * 64,
        "expires_in_seconds": 60,
    }
    response = client.post(
        "/agent/host-runtime/grant",
        headers=agent_headers(NODE_A, "serial-a"),
        json=request,
    )
    assert response.status_code == 200
    assert response.content == canonical_message(response.json())
    grant = SignedHostHelperGrant.parse(response.json()["grant"])
    grant_bytes = canonical_message(grant.to_mapping()) + b"\n"

    produced = subprocess.run(
        [str(host_helper_wire_probe)],
        input=grant_bytes,
        capture_output=True,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr.decode()
    receipt_raw = produced.stdout.rstrip(b"\n")
    receipt = SignedRecipeRunObservationReceipt.parse(json.loads(receipt_raw))
    assert canonical_message(receipt.to_mapping()) == receipt_raw
    receipt_public_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes([23]) * 32
    ).public_key()
    receipt_public_key.verify(
        bytes.fromhex(receipt.signature.value),
        recipe_run_observation_receipt_signing_bytes(receipt.claims),
    )
    assert receipt.claims.request_id == grant.claims.request_id
    assert receipt.claims.request_sha256 == request["request_sha256"]
    assert receipt.claims.observation_identity_sha256 is not None
