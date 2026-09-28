from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.host_helper import (
    ConfirmPackageActivationOperation,
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    RecipeReconciliationIdentity,
    SignedHostHelperGrant,
)
from vonk_control.host_helper_authority import HostHelperGrantIssuer

from tests.wire_probes import prebuilt_probe

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
    return prebuilt_probe("VONK_HOST_HELPER_WIRE_PROBE")


def test_python_issuer_matches_the_rust_verified_host_grant_fixture() -> None:
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED),
        clock=lambda: datetime.fromtimestamp(2_100_000_000, UTC),
        request_id_factory=lambda: "10000000-0000-4000-8000-000000000001",
    )
    grant = issuer.issue_grant(
        node_id="spk_11111111111111111111111111111111",
        operation=ConfirmPackageActivationOperation(
            type="confirm-package-activation",
            package_sha256="a" * 64,
            attempt_nonce="b" * 64,
        ),
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
            start_plan_sha256: str | None = None,
            stop_plan_sha256: str | None = None,
            run_generation: int | None = None,
            runtime_run_id: str | None = None,
            runtime_target_id: str | None = None,
            runtime_installation_id: str | None = None,
            installation_id: str | None = None,
            reconciliation_identity: RecipeReconciliationIdentity | None = None,
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
                    start_plan_sha256=start_plan_sha256,
                    stop_plan_sha256=stop_plan_sha256,
                    run_generation=run_generation,
                    runtime_run_id=runtime_run_id,
                    runtime_target_id=runtime_target_id,
                    runtime_installation_id=runtime_installation_id,
                ),
                expires_in_seconds=expires_in_seconds,
            )

    object.__setattr__(services, "host_runtime_authority", RecordingHostAuthority())
    request = {
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
    assert produced.stdout == grant_bytes


def test_python_reconciliation_grant_is_verified_unchanged_by_rust(
    host_helper_wire_probe: Path,
) -> None:
    identity = RecipeReconciliationIdentity(
        installation_id="70000000-0000-4000-8000-000000000007",
        plan_digest="b" * 64,
    )
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED),
        clock=lambda: datetime.fromtimestamp(2_100_000_000, UTC),
        request_id_factory=lambda: "10000000-0000-4000-8000-000000000001",
    )
    grant = issuer.issue_grant(
        node_id=NODE_A,
        operation=ExecuteContainerRuntimeRequestOperation(
            type="execute-container-runtime-request",
            action="installation-cleanup",
            fence="40000000-0000-4000-8000-000000000004",
            request_sha256="e" * 64,
            installation_id=identity.installation_id,
            reconciliation_identity=identity,
        ),
        expires_in_seconds=60,
    )
    raw = canonical_message(grant)
    verified = subprocess.run(
        [str(host_helper_wire_probe)], input=raw, capture_output=True, check=False
    )
    assert verified.returncode == 0, verified.stderr.decode()
    assert verified.stdout.rstrip(b"\n") == raw
    tampered = json.loads(raw)
    tampered["claims"]["operation"]["reconciliation_identity"]["plan_digest"] = "f" * 64
    refused = subprocess.run(
        [str(host_helper_wire_probe)],
        input=canonical_message(tampered),
        capture_output=True,
        check=False,
    )
    assert refused.returncode != 0
