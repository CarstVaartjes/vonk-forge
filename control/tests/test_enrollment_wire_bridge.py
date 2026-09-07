"""Controller enrollment and renewal through the production Rust wire types."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent_protocol.enrollment import (
    EnrollmentBootstrapResponse,
    EnrollmentSubmitRequest,
    IssuedCertificateResponse,
)

from .test_agent_api import (
    NODE_A,
    NODE_C,
    _csr_for,
    agent_headers,
    enrollment_grant,
    valid_enrollment_body,
)
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def bootstrap_wire_probe() -> Path:
    configured = os.environ.get("VONK_BOOTSTRAP_WIRE_PROBE")
    if not configured:
        raise AssertionError(
            "run scripts/tests/run_agent_wire_contracts.py to build wire probes"
        )
    probe = Path(configured).resolve()
    assert probe.is_file() and os.access(probe, os.X_OK)
    return probe


@pytest.fixture(scope="session")
def enrollment_wire_probe() -> Path:
    configured = os.environ.get("VONK_ENROLLMENT_WIRE_PROBE")
    if not configured:
        raise AssertionError(
            "run scripts/tests/run_agent_wire_contracts.py to build wire probes"
        )
    probe = Path(configured).resolve()
    assert probe.is_file() and os.access(probe, os.X_OK)
    return probe


def _roundtrip(probe: Path, content: bytes, *arguments: str) -> bytes:
    result = subprocess.run(
        [str(probe), *arguments],
        input=content + b"\n",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


@pytest.mark.parametrize("private_address", [True, False])
def test_controller_bootstrap_uses_the_setup_parser(
    agent_system, bootstrap_wire_probe: Path, private_address: bool
) -> None:
    from dataclasses import replace

    from pydantic import ValidationError

    client, services, _, _ = agent_system
    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )
    if not private_address:
        object.__setattr__(
            services,
            "bootstrap",
            replace(services.bootstrap, controller_address=None, service_hostnames=()),
        )
    response = client.get("/agent/v1/bootstrap")
    assert response.status_code == 200
    expected = EnrollmentBootstrapResponse.model_validate_json(response.content)
    returned = _roundtrip(bootstrap_wire_probe, response.content)
    assert EnrollmentBootstrapResponse.model_validate_json(returned) == expected

    # Every required field stays required through the actual setup parser,
    # including the explicitly nullable address and empty hostname list.
    for field in EnrollmentBootstrapResponse.model_json_schema()["required"]:
        incomplete = response.json()
        del incomplete[field]
        with pytest.raises(ValidationError):
            EnrollmentBootstrapResponse.model_validate(incomplete)
        result = subprocess.run(
            [str(bootstrap_wire_probe)],
            input=json.dumps(incomplete).encode() + b"\n",
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, field
    operation = client.app.openapi()["paths"]["/agent/v1/bootstrap"]["get"]
    assert not any(p["name"] == "setup_schema" for p in operation.get("parameters", []))
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EnrollmentBootstrapResponse"
    }


def test_rust_enrollment_request_and_controller_issued_response(
    agent_system, enrollment_wire_probe: Path
) -> None:
    client, services, _, _ = agent_system
    authored = EnrollmentSubmitRequest.model_validate_json(
        valid_enrollment_body(enrollment_grant(services))
    )
    request = _roundtrip(
        enrollment_wire_probe, authored.model_dump_json().encode(), "--request"
    )
    assert EnrollmentSubmitRequest.model_validate_json(request) == authored
    response = client.post("/agent/v1/enroll", json=json.loads(request))
    assert response.status_code == 200
    issued = IssuedCertificateResponse.model_validate_json(response.content)
    returned = _roundtrip(enrollment_wire_probe, response.content, "--issued", NODE_C)
    assert IssuedCertificateResponse.model_validate_json(returned) == issued
    schema = client.app.openapi()["paths"]["/agent/v1/enroll"]["post"]
    assert schema["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/IssuedCertificateResponse"
    }


def test_controller_renewal_uses_the_same_issued_response(
    agent_system, enrollment_wire_probe: Path
) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
    )
    assert response.status_code == 200
    issued = IssuedCertificateResponse.model_validate_json(response.content)
    returned = _roundtrip(enrollment_wire_probe, response.content, "--issued", NODE_A)
    assert IssuedCertificateResponse.model_validate_json(returned) == issued


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_issued_generation_rejects_coercible_values(value: object) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        IssuedCertificateResponse.model_validate(
            {
                "node_id": NODE_A,
                "certificate_pem": "certificate",
                "chain_pem": "chain",
                "serial": "1",
                "fingerprint": "a" * 64,
                "not_before": "2026-09-07T00:00:00+00:00",
                "not_after": "2027-09-07T00:00:00+00:00",
                "generation": value,
            }
        )
