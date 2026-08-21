from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance.runtime import AcceptanceError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/acceptance/test_fresh_nas_install.py"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("fresh_nas_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_entrypoint_resolves_repository_imports() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("VONK_ACCEPTANCE_")
    }

    result = subprocess.run(
        [sys.executable, SCRIPT],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError" not in result.stderr
    assert result.stderr.startswith("fresh NAS acceptance: ")


def test_only_hermes_bundle_receives_the_expensive_reference_rollout(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    default = tmp_path / "default"
    hermes = tmp_path / "hermes"

    assert acceptance.reference_rollout_bundles(default, hermes) == (hermes,)


def test_generate_bundle_allows_the_installer_to_reuse_its_target(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    target = tmp_path / "target"

    def install(_command, *, cwd, **_kwargs):
        (cwd / "vonk-forge").mkdir(exist_ok=True)

    monkeypatch.setattr(acceptance, "run_interactive", install)
    monkeypatch.setattr(acceptance, "assert_bundle_contract", lambda _bundle: None)

    arguments = {
        "candidate_url": "https://install.example/bootstraps/nas",
        "child_environment": {},
        "responses": [],
    }
    first = acceptance.generate_bundle(target, **arguments)
    second = acceptance.generate_bundle(
        target, **arguments, require_all_prompts=False
    )

    assert first == second == target / "vonk-forge"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("dev-203-g479daeacb4a0", True),
        ("0.1.0-dev.203", True),
        ("dev", False),
        ("latest", False),
    ],
)
def test_immutable_image_check_distinguishes_versioned_dev_tags(
    tag: str, expected: bool
) -> None:
    acceptance = _acceptance_module()
    image = f"ghcr.io/carstvaartjes/vonk-forge-api:{tag}@sha256:{'a' * 64}"

    assert acceptance.is_immutable_image(image) is expected
    assert not acceptance.is_immutable_image(
        "ghcr.io/carstvaartjes/vonk-forge-api:0.1.0"
    )


def _serve_status(*, hermes: bool) -> dict[str, object]:
    services: dict[str, object] = {
        "svc:vonk-forge": {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "vonk-forge.acceptance.example.test:443": {
                    "Handlers": {"/": {"Proxy": "http://caddy:8080"}}
                }
            },
        }
    }
    if hermes:
        services.update(
            {
                "svc:hermes-api": {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "hermes-api.acceptance.example.test:443": {
                            "Handlers": {
                                "/": {"Proxy": "http://hermes-agent:8642"}
                            }
                        }
                    },
                },
                "svc:hermes-dashboard": {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "hermes-dashboard.acceptance.example.test:443": {
                            "Handlers": {
                                "/": {"Proxy": "http://hermes-agent:9119"}
                            }
                        }
                    },
                },
            }
        )
    return {"Services": services}


def _serve_configuration(*, hermes: bool) -> dict[str, object]:
    services: dict[str, object] = {
        "svc:vonk-forge": {"endpoints": {"tcp:443": "http://caddy:8080"}}
    }
    if hermes:
        services.update(
            {
                "svc:hermes-api": {
                    "endpoints": {"tcp:443": "http://hermes-agent:8642"}
                },
                "svc:hermes-dashboard": {
                    "endpoints": {"tcp:443": "http://hermes-agent:9119"}
                },
            }
        )
    return {"version": "0.0.1", "services": services}


def test_tailnet_serve_configuration_requires_exact_selected_upstreams() -> None:
    acceptance = _acceptance_module()
    status = _serve_status(hermes=True)
    configuration = _serve_configuration(hermes=True)

    acceptance.assert_tailnet_serve_configuration(
        json.dumps(status),
        json.dumps(configuration),
        hermes=True,
        tailnet_suffix="acceptance.example.test",
    )

    invalid: list[dict[str, object]] = []
    extra_service = _serve_configuration(hermes=True)
    extra_service["services"]["svc:unexpected"] = {  # type: ignore[index]
        "endpoints": {"tcp:443": "http://unexpected:9999"}
    }
    invalid.append(extra_service)
    missing_service = _serve_configuration(hermes=True)
    del missing_service["services"]["svc:hermes-dashboard"]  # type: ignore[index]
    invalid.append(missing_service)
    wrong_target = _serve_configuration(hermes=True)
    wrong_target["services"]["svc:hermes-api"]["endpoints"]["tcp:443"] = (  # type: ignore[index]
        "http://caddy:8080"
    )
    invalid.append(wrong_target)
    wrong_port = _serve_configuration(hermes=True)
    wrong_port["services"]["svc:vonk-forge"]["endpoints"] = {  # type: ignore[index]
        "tcp:8443": "http://caddy:8080"
    }
    invalid.append(wrong_port)
    extra_route = _serve_configuration(hermes=True)
    extra_route["services"]["svc:vonk-forge"]["endpoints"]["tcp:80"] = (  # type: ignore[index]
        "http://caddy:8080"
    )
    invalid.append(extra_route)

    for document in invalid:
        with pytest.raises(AcceptanceError, match="Serve configuration"):
            acceptance.assert_tailnet_serve_configuration(
                json.dumps(status),
                json.dumps(document),
                hermes=True,
                tailnet_suffix="acceptance.example.test",
            )


def test_tailnet_serve_status_requires_the_exact_selected_routes() -> None:
    acceptance = _acceptance_module()
    default = _serve_status(hermes=False)
    hermes = _serve_status(hermes=True)

    acceptance.assert_tailnet_serve_status(
        json.dumps(default), hermes=False, tailnet_suffix="acceptance.example.test"
    )
    acceptance.assert_tailnet_serve_status(
        json.dumps(hermes), hermes=True, tailnet_suffix="acceptance.example.test"
    )

    invalid: list[dict[str, object]] = []
    extra_service = _serve_status(hermes=True)
    extra_service["Services"]["svc:unexpected"] = {  # type: ignore[index]
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"unexpected.acceptance.example.test:443": {"Handlers": {"/": {"Proxy": "http://unexpected:9999"}}}},
    }
    invalid.append(extra_service)
    missing_service = _serve_status(hermes=True)
    del missing_service["Services"]["svc:hermes-dashboard"]  # type: ignore[index]
    invalid.append(missing_service)
    wrong_target = _serve_status(hermes=True)
    wrong_target["Services"]["svc:hermes-api"]["Web"][  # type: ignore[index]
        "hermes-api.acceptance.example.test:443"
    ]["Handlers"]["/"]["Proxy"] = "http://caddy:8080"  # type: ignore[index]
    invalid.append(wrong_target)
    wrong_port = _serve_status(hermes=True)
    wrong_port["Services"]["svc:vonk-forge"]["TCP"] = {"8443": {"HTTPS": True}}  # type: ignore[index]
    invalid.append(wrong_port)
    wrong_protocol = _serve_status(hermes=True)
    wrong_protocol["Services"]["svc:vonk-forge"]["TCP"]["443"] = {"HTTP": True}  # type: ignore[index]
    invalid.append(wrong_protocol)
    node_listener = _serve_status(hermes=True)
    node_listener["TCP"] = {"443": {"HTTPS": True}}
    invalid.append(node_listener)

    for document in invalid:
        with pytest.raises(AcceptanceError, match="Serve status"):
            acceptance.assert_tailnet_serve_status(
                json.dumps(document),
                hermes=True,
                tailnet_suffix="acceptance.example.test",
            )
    with pytest.raises(AcceptanceError, match="Serve status"):
        acceptance.assert_tailnet_serve_status(
            '{"Services":{},"Services":{}}',
            hermes=False,
            tailnet_suffix="acceptance.example.test",
        )


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
