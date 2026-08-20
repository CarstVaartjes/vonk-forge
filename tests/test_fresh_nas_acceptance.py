from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.acceptance.runtime import AcceptanceError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/acceptance/test_fresh_nas_install.py"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("fresh_nas_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_hermes_bundle_receives_the_expensive_reference_rollout(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    default = tmp_path / "default"
    hermes = tmp_path / "hermes"

    assert acceptance.reference_rollout_bundles(default, hermes) == (hermes,)


def test_routed_service_checks_require_authentication_and_expected_data(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    fixture = tmp_path / "compose"
    fixture.write_text("#!/bin/sh\n")
    fixture.chmod(0o755)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in {
        "litellm-master-key": "litellm-secret",
        "grafana-admin-password": "grafana-secret",
        "step-ca/root-certificate": "root",
    }.items():
        target = secrets / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("VONK_ACCEPTANCE_REFERENCE_COMPOSE", str(fixture))

    client_certificate = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    client_certificate.write_text("certificate")
    client_key.write_text("key")
    monkeypatch.setattr(
        acceptance,
        "issue_registry_client_certificate",
        lambda *_: (client_certificate, client_key),
    )

    def request(command: list[str], **kwargs: object) -> bytes:
        calls.append((command, kwargs))
        path = kwargs["path"]
        headers = kwargs.get("headers", {})
        assert isinstance(path, str)
        assert isinstance(headers, dict)
        if kwargs.get("server_hostname") == "registry.acceptance.example.test":
            if kwargs.get("client_certificate") is None:
                raise AcceptanceError("registry requires a client certificate")
            assert path == "/v2/"
            return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}"
        if path == "/v1/models":
            status = "200 OK" if headers.get("Authorization") == "Bearer litellm-secret" else "401 Unauthorized"
            assert int(status[:3]) in kwargs["accepted_statuses"]
            return f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\r\n{{\"data\":[]}}".encode()
        if path == "/grafana/api/user":
            status = (
                "200 OK"
                if headers.get("Authorization") == "Basic YWRtaW46Z3JhZmFuYS1zZWNyZXQ="
                else "401 Unauthorized"
            )
            assert int(status[:3]) in kwargs["accepted_statuses"]
            return f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\r\n{{\"login\":\"admin\"}}".encode()
        if path == "/grafana/api/datasources/uid/vonk-prometheus":
            return b'HTTP/1.1 200 OK\r\n\r\n{"uid":"vonk-prometheus","type":"prometheus"}'
        if path.startswith("/grafana/api/datasources/uid/vonk-prometheus/resources/api/v1/query?"):
            return b'HTTP/1.1 200 OK\r\n\r\n{"status":"success","data":{"resultType":"vector","result":[{"metric":{"job":"vonk-control"},"value":["1","1"]}]}}'
        if path == "/grafana/api/search?query=Vonk%20Forge":
            return b'HTTP/1.1 200 OK\r\n\r\n[{"uid":"vonk-fleet"},{"uid":"vonk-jobs"}]'
        raise AssertionError(path)

    monkeypatch.setattr(acceptance, "https_over_command", request)

    acceptance.verify_routed_service_behavior(
        tmp_path,
        nas_ip="192.0.2.20",
        control_hostname="vonk-forge.acceptance.example.test",
        registry_hostname="registry.acceptance.example.test",
    )

    requests = {kwargs["path"]: kwargs for _, kwargs in calls}
    models = requests["/v1/models"]
    assert models["headers"] == {"Authorization": "Bearer litellm-secret"}
    assert models["accepted_statuses"] == {200}
    assert any(
        path.startswith(
            "/grafana/api/datasources/uid/vonk-prometheus/resources/api/v1/query?"
        )
        for path in requests
    )
    assert requests["/grafana/api/user"]["headers"]["Authorization"].startswith("Basic ")
    registry = next(kwargs for _, kwargs in calls if kwargs["server_hostname"] == "registry.acceptance.example.test" and kwargs.get("client_certificate") is not None)
    assert registry["ca_file"] == secrets / "step-ca/root-certificate"
    assert registry["client_certificate"] == client_certificate
