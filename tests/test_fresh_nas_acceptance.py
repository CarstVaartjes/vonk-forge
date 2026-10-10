from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
import yaml

from tests.acceptance.runtime import AcceptanceError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/acceptance/test_fresh_nas_install.py"
PAYLOAD_BUILDER = ROOT / "scripts/build-nas-compose-bundle"
PRODUCTION_RENDERER = ROOT / "scripts/render-production-compose"
COMPOSE_TEMPLATE = ROOT / "deploy/compose/compose.yaml"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("fresh_nas_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_module(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
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


def test_canonical_hermes_topology_includes_the_key_provisioner() -> None:
    acceptance = _acceptance_module()

    assert acceptance.HERMES_SERVICES == acceptance.DEFAULT_SERVICES | {
        "hermes-agent",
        "hermes-litellm-key-provisioner",
    }


def test_nas_responses_answer_only_the_remaining_installer_prompts() -> None:
    acceptance = _acceptance_module()
    arguments = {
        "nas_ip": "192.0.2.10",
        "tailnet_suffix": "acceptance.example.test",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
        "upstream_key": "upstream-key",
        "control_service": "svc:vonk-forge-ci",
    }

    disabled = dict(acceptance.nas_responses(**arguments, hermes=False))
    enabled = dict(acceptance.nas_responses(**arguments, hermes=True))
    assert set(disabled) == set(enabled)
    assert {disabled[prompt] for prompt in disabled if "Hermes" in prompt} == {"n"}
    assert {enabled[prompt] for prompt in enabled if "Hermes" in prompt} == {"y"}
    answers = set(disabled.values())
    assert {"secure-remote", "192.0.2.10", "vonk-forge-ci.acceptance.example.test"} <= (
        answers
    )
    # CIDRs, derived hostnames, and every internal secret are never asked.
    assert not any("CIDR" in prompt or "password" in prompt for prompt in disabled)


@pytest.mark.parametrize("hermes", [False, True])
def test_installer_output_detects_permuted_secret_answers(
    tmp_path: Path, hermes: bool
) -> None:
    acceptance = _acceptance_module()
    arguments = {
        "nas_ip": "192.0.2.10",
        "tailnet_suffix": "acceptance.example.test",
        "oauth_client_id": "distinct-client-id",
        "oauth_client_secret": "distinct-client-secret",
        "upstream_key": "distinct-upstream-key",
        "control_service": "svc:vonk-forge-ci",
        "hermes_dashboard_service": "svc:hermes-dashboard-ci",
        "hermes": hermes,
    }
    profiles = '"secure-remote,hermes"' if hermes else "secure-remote"
    (tmp_path / ".env").write_text(
        "NAS_LAN_IP=192.0.2.10\n"
        "VONK_CONTROL_HOSTNAME=vonk-forge-ci.acceptance.example.test\n"
        f"COMPOSE_PROFILES={profiles}\n"
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in {
        "tailscale-oauth-client-id": "distinct-client-id\n",
        "tailscale-oauth-client-secret": "distinct-client-secret\n",
        "litellm-upstream-key": "distinct-upstream-key\n",
        "hf-token": "",
    }.items():
        (secrets / name).write_text(value)
    acceptance.assert_installer_answer_bindings(tmp_path, **arguments)

    # Individually valid answers still have distinct owners; an unordered answer
    # set cannot detect their permutation at the consumer's output boundary.
    (secrets / "tailscale-oauth-client-id").write_text("distinct-client-secret\n")
    (secrets / "tailscale-oauth-client-secret").write_text("distinct-client-id\n")
    with pytest.raises(AcceptanceError):
        acceptance.assert_installer_answer_bindings(tmp_path, **arguments)
    (secrets / "tailscale-oauth-client-id").write_text("distinct-client-id\n")
    (secrets / "tailscale-oauth-client-secret").write_text("distinct-client-secret\n")
    acceptance.assert_installer_answer_bindings(tmp_path, **arguments)


def test_generate_bundle_allows_the_installer_to_reuse_its_target(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    target = tmp_path / "target"

    def install(_command, *, cwd, **_kwargs):
        (cwd / "vonk-forge").mkdir(exist_ok=True)

    monkeypatch.setattr(acceptance, "run", install)
    monkeypatch.setattr(acceptance, "assert_bundle_contract", lambda _bundle: None)

    arguments = {
        "candidate_url": "https://install.example/bootstraps/nas",
        "child_environment": {},
        "responses": [],
    }
    first = acceptance.generate_bundle(target, **arguments)
    second = acceptance.generate_bundle(target, **arguments, require_all_prompts=False)

    assert first == second == target / "vonk-forge"


def test_nas_bind_address_can_differ_from_the_reachable_address(monkeypatch) -> None:
    acceptance = _acceptance_module()

    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.delenv("VONK_ACCEPTANCE_NAS_BIND_IP", raising=False)
    assert acceptance.nas_bind_ipv4("172.18.0.2") == "0.0.0.0"

    monkeypatch.setenv("VONK_ACCEPTANCE_NAS_BIND_IP", "0.0.0.0")
    assert acceptance.nas_bind_ipv4("172.18.0.2") == "0.0.0.0"

    monkeypatch.setenv("VONK_ACCEPTANCE_NAS_BIND_IP", "ff02::1")
    with pytest.raises(AcceptanceError):
        acceptance.nas_bind_ipv4("172.18.0.2")


def test_host_commands_preserve_only_explicit_docker_connection(monkeypatch) -> None:
    acceptance = _acceptance_module()
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    environment = acceptance.host_command_environment()

    assert environment["DOCKER_HOST"] == "tcp://127.0.0.1:2375"
    assert environment["DOCKER_TLS_VERIFY"] == "1"
    assert "UNRELATED_SECRET" not in environment


def test_tailnet_client_requirement_is_explicit_and_fail_closed(monkeypatch) -> None:
    acceptance = _acceptance_module()
    monkeypatch.delenv("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", raising=False)
    assert acceptance.require_tailnet_client() is True

    monkeypatch.setenv("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", "false")
    assert acceptance.require_tailnet_client() is False

    monkeypatch.setenv("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", "False")
    with pytest.raises(AcceptanceError):
        acceptance.require_tailnet_client()


def test_tailscale_disabled_mode_refuses_credentials_and_gateway_ownership(
    monkeypatch,
) -> None:
    acceptance = _acceptance_module()
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_MODE", "disabled")
    monkeypatch.delenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", raising=False)

    assert acceptance.tailscale_acceptance_mode() == "disabled"
    assert acceptance.tailscale_acceptance_credentials("disabled") == (
        "tailscale-disabled-client",
        "tailscale-disabled-secret",
    )

    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID", "forbidden")
    with pytest.raises(AcceptanceError):
        acceptance.tailscale_acceptance_credentials("disabled")

    monkeypatch.delenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID")
    monkeypatch.setenv("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", "false")
    with pytest.raises(AcceptanceError):
        acceptance.tailscale_acceptance_credentials("disabled")

    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_MODE", "other")
    with pytest.raises(AcceptanceError):
        acceptance.tailscale_acceptance_mode()


def test_full_tailscale_acceptance_requires_isolated_disposable_tailnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acceptance = _acceptance_module()
    monkeypatch.delenv("VONK_ACCEPTANCE_TAILNET_KIND", raising=False)
    with pytest.raises(AcceptanceError):
        acceptance.assert_tailscale_acceptance_boundary("full")

    monkeypatch.setenv("VONK_ACCEPTANCE_TAILNET_KIND", acceptance.ISOLATED_TAILNET_KIND)
    acceptance.assert_tailscale_acceptance_boundary("full")

    with pytest.raises(AcceptanceError):
        acceptance.assert_tailscale_acceptance_boundary("disabled")

    monkeypatch.delenv("VONK_ACCEPTANCE_TAILNET_KIND")
    acceptance.assert_tailscale_acceptance_boundary("disabled")


def test_operator_tailscale_assets_have_no_acceptance_service_or_policy() -> None:
    operator_assets = (
        ROOT / "deploy/compose/tailscale/compose.yaml",
        ROOT / "deploy/compose/tailscale/configure.sh",
        ROOT / "deploy/compose/tailscale/grants.example.hujson",
    )
    forbidden = (
        "svc:vonk-forge-acceptance",
        "svc:hermes-api-acceptance",
        "svc:hermes-dashboard-acceptance",
        "tag:vonk-acceptance",
    )
    for path in operator_assets:
        text = path.read_text(encoding="utf-8")
        assert all(item not in text for item in forbidden), path


def test_tailscale_disabled_service_set_excludes_both_owners() -> None:
    acceptance = _acceptance_module()

    assert acceptance.TAILSCALE_SERVICES == {
        "tailscale-configurator",
        "tailscale-gateway",
    }
    assert acceptance.LOCAL_SERVICES == (
        acceptance.DEFAULT_SERVICES - acceptance.TAILSCALE_SERVICES
    )
    assert acceptance.LOCAL_HERMES_SERVICES == (
        acceptance.HERMES_SERVICES - acceptance.TAILSCALE_SERVICES
    )


def test_tailscale_disabled_ps_contract_rejects_either_owner() -> None:
    acceptance = _acceptance_module()
    healthy = [
        {
            "ExitCode": 0,
            "Health": "healthy",
            "Service": service,
            "State": "running",
        }
        for service in sorted(acceptance.LOCAL_HERMES_SERVICES)
    ]

    acceptance.assert_tailscale_services_absent(json.dumps(healthy))
    for service in sorted(acceptance.TAILSCALE_SERVICES):
        with pytest.raises(AcceptanceError):
            acceptance.assert_tailscale_services_absent(
                json.dumps(healthy + [{"Service": service}])
            )


def test_tailscale_disabled_rollout_starts_only_the_local_service_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _acceptance_module()
    compose = ["docker", "compose"]
    calls: list[list[str]] = []
    source = "a" * 40
    monkeypatch.setenv("VONK_ACCEPTANCE_SOURCE_SHA", source)
    package_identity = {
        "source_sha": source,
        "control_contract_sha256": "b" * 64,
        "worker_contract_sha256": "c" * 64,
    }
    healthy = json.dumps(
        [
            {
                "ExitCode": 0,
                "Health": "healthy",
                "Service": service,
                "State": "running",
            }
            for service in sorted(acceptance.LOCAL_HERMES_SERVICES)
        ]
    )

    def run(command, **_kwargs):
        calls.append(command)
        if command[-3:] == ["config", "--images"]:
            output = "example.invalid/image@sha256:" + "a" * 64 + "\n"
        elif command[-4:] == ["ps", "--all", "--format", "json"]:
            output = healthy
        elif command[2:6] == ["exec", "-T", "control-api", "python"]:
            output = json.dumps(
                {
                    "package": package_identity,
                    "observation": {
                        "api": package_identity,
                        "workers": [package_identity],
                        "worker_issue": None,
                    },
                }
            )
        elif command[2:6] == ["exec", "-T", "control-worker", "python"]:
            output = json.dumps(package_identity)
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    observed: dict[str, object] = {}
    monkeypatch.setattr(acceptance, "reference_compose", lambda: compose)
    monkeypatch.setattr(
        acceptance,
        "compose_services",
        lambda _bundle: acceptance.HERMES_SERVICES,
    )
    monkeypatch.setattr(acceptance, "run", run)
    monkeypatch.setattr(
        acceptance,
        "assert_compose_services_healthy",
        lambda _raw, expected: observed.update(expected=expected),
    )
    monkeypatch.setattr(acceptance, "verify_controller_tls", lambda *_args: None)
    monkeypatch.setattr(acceptance, "verify_postgres_databases", lambda *_args: None)
    monkeypatch.setattr(
        acceptance,
        "verify_tailscale_services",
        lambda *_args, **_kwargs: pytest.fail("Tailscale verification was reached"),
    )

    acceptance.exercise_compose(
        tmp_path,
        nas_ip="127.0.0.1",
        control_hostname="control.acceptance.example.test",
        enrollment_hostname="enroll.acceptance.example.test",
        registry_hostname="registry.acceptance.example.test",
        tailnet_suffix="acceptance.example.test",
        hermes=True,
        tailscale_mode="disabled",
    )

    up = next(command for command in calls if "up" in command)
    assert up[-len(acceptance.LOCAL_HERMES_SERVICES) :] == sorted(
        acceptance.LOCAL_HERMES_SERVICES
    )
    assert not set(up) & acceptance.TAILSCALE_SERVICES
    assert observed["expected"] == acceptance.LOCAL_HERMES_SERVICES
    assert {command[4] for command in calls if command[2:4] == ["exec", "-T"]} == {
        "control-api",
        "control-worker",
    }
    assert any("down" in command for command in calls)


def test_nas_startup_diagnostics_identify_unhealthy_service_and_redact_secret(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    status = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            [
                {
                    "ExitCode": 0,
                    "Health": "healthy",
                    "Service": "postgres",
                    "State": "running",
                },
                {
                    "ExitCode": 1,
                    "Health": "unhealthy",
                    "Service": "tailscale-configurator",
                    "State": "running",
                },
            ]
        ),
        stderr="",
    )
    logs = subprocess.CompletedProcess(
        [],
        0,
        stdout=f"authentication failed for {secret}\n",
        stderr="",
    )
    outputs = iter((status, logs))
    monkeypatch.setattr(
        acceptance, "_diagnostic_command", lambda _bundle, _command: next(outputs)
    )
    monkeypatch.setattr(acceptance, "reference_compose", lambda: ["docker", "compose"])

    diagnostics = acceptance.compose_startup_diagnostics(tmp_path)

    assert secret not in diagnostics
    assert "authentication failed for <redacted>" in diagnostics
    assert "postgres=running/healthy/exit-0" in diagnostics
    assert "tailscale-configurator=running/unhealthy/exit-1" in diagnostics

    cause = acceptance._redact_diagnostics(f"image pull failed while using {secret}")
    assert cause == "image pull failed while using <redacted>"


def test_acceptance_service_override_edits_only_the_bundle_compose(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".env").write_text(
        "VONK_CONTROL_HOSTNAME=vonk-forge.acceptance.example.test\n", encoding="utf-8"
    )
    compose = bundle / "docker-compose.yaml"
    compose.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "tailscale-gateway": {
                        "hostname": "vonk-forge-gateway",
                        "environment": {"TS_HOSTNAME": "vonk-forge-gateway"},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "printf '?ephemeral=false&preauthorized=true'",
                        ],
                    },
                    "tailscale-configurator": {"environment": {}},
                    "hermes-agent": {"environment": {}},
                }
            }
        ),
        encoding="utf-8",
    )
    compose.chmod(0o640)

    acceptance.configure_tailnet_service_names(
        bundle,
        control="svc:vonk-forge",
        gateway_hostname="vonk-forge-ci-123-1",
        hermes_api="svc:hermes-api",
        hermes_dashboard="svc:hermes-dashboard-ci",
        require_external_tailnet_client=False,
    )

    assert compose.stat().st_mode & 0o777 == 0o640
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    gateway = services["tailscale-gateway"]
    assert gateway["hostname"] == "vonk-forge-ci-123-1"
    assert gateway["environment"]["TS_HOSTNAME"] == "vonk-forge-ci-123-1"
    assert "?ephemeral=true&preauthorized=true" in gateway["command"][-1]
    assert services["tailscale-configurator"]["environment"] == {
        "VONK_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge",
        "VONK_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api",
        "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard-ci",
        "VONK_TAILSCALE_EPHEMERAL": "true",
        "TS_REQUIRE_PRIMARY_ROUTES": "0",
        "TS_REQUIRE_SERVICE_HOST": "0",
    }
    assert services["hermes-agent"]["environment"]["API_SERVER_CORS_ORIGINS"] == (
        "https://hermes-dashboard-ci.acceptance.example.test"
    )
    assert (
        acceptance.tailscale_service_hostname(
            "svc:vonk-forge", "acceptance.example.test"
        )
        == "vonk-forge.acceptance.example.test"
    )

    with pytest.raises(AcceptanceError):
        acceptance.configure_tailnet_service_names(
            bundle,
            control="svc:duplicate",
            gateway_hostname="vonk-forge-ci-123-1",
            hermes_api="svc:duplicate",
            hermes_dashboard="svc:hermes-dashboard",
            require_external_tailnet_client=False,
        )

    with pytest.raises(AcceptanceError):
        acceptance.configure_tailnet_service_names(
            bundle,
            control="svc:vonk-forge",
            gateway_hostname="INVALID HOSTNAME",
            hermes_api="svc:hermes-api",
            hermes_dashboard="svc:hermes-dashboard",
            require_external_tailnet_client=False,
        )


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
    # A release tag without a digest pins an explicit version, so it is
    # immutable; a floating channel tag is not.
    assert acceptance.is_immutable_image("ghcr.io/carstvaartjes/vonk-forge-api:0.1.0")
    assert not acceptance.is_immutable_image(
        "ghcr.io/carstvaartjes/vonk-forge-api:latest"
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
                            "Handlers": {"/": {"Proxy": "http://hermes-agent:8642"}}
                        }
                    },
                },
                "svc:hermes-dashboard": {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "hermes-dashboard.acceptance.example.test:443": {
                            "Handlers": {"/": {"Proxy": "http://hermes-agent:9119"}}
                        }
                    },
                },
            }
        )
    return {"Services": services}


class _TailscaleStatus(TypedDict):
    BackendState: str
    Self: dict[str, object]


class _RequestKwargs(TypedDict):
    server_hostname: str
    timeout: float
    accepted_statuses: set[int]
    path: str
    headers: dict[str, str]
    client_certificate: Path | None
    ca_file: Path


def _tailscale_status(*, hermes: bool) -> _TailscaleStatus:
    service_addresses = {"svc:vonk-forge": ["100.64.0.10"]}
    if hermes:
        service_addresses.update(
            {
                "svc:hermes-api": ["100.64.0.11"],
                "svc:hermes-dashboard": ["100.64.0.12"],
            }
        )
    return {
        "BackendState": "Running",
        "Self": {
            "CapMap": {"service-host": [service_addresses]},
            "PrimaryRoutes": [
                f"{address}/32"
                for addresses in service_addresses.values()
                for address in addresses
            ],
        },
    }


def test_tailnet_service_route_ownership_is_bound_to_the_current_gateway() -> None:
    acceptance = _acceptance_module()
    expected = {"svc:vonk-forge", "svc:hermes-api", "svc:hermes-dashboard"}
    valid = _tailscale_status(hermes=True)
    acceptance.assert_tailnet_service_primary_routes(
        valid,
        expected_services=expected,
    )

    absent = {"BackendState": "Running"}
    missing_mapping = json.loads(json.dumps(valid))
    missing_mapping["Self"]["CapMap"].pop("service-host")
    missing = json.loads(json.dumps(valid))
    missing["Self"]["PrimaryRoutes"].remove("100.64.0.12/32")
    mismatched = json.loads(json.dumps(valid))
    mismatched["Self"]["PrimaryRoutes"][-1] = "100.64.0.99/32"
    wrong_service = json.loads(json.dumps(valid))
    service_hosts = wrong_service["Self"]["CapMap"]["service-host"][0]
    service_hosts["svc:unrelated"] = service_hosts.pop("svc:hermes-dashboard")
    other_host_only = json.loads(json.dumps(valid))
    other_host_only["Peer"] = {
        "other": {"PrimaryRoutes": other_host_only["Self"]["PrimaryRoutes"]}
    }
    other_host_only["Self"]["PrimaryRoutes"] = []
    duplicate = json.loads(json.dumps(valid))
    duplicate["Self"]["PrimaryRoutes"].append("100.64.0.10/32")

    for status in (
        absent,
        missing_mapping,
        missing,
        mismatched,
        wrong_service,
        other_host_only,
        duplicate,
    ):
        with pytest.raises(AcceptanceError):
            acceptance.assert_tailnet_service_primary_routes(
                status,
                expected_services=expected,
            )


def test_tailnet_service_mapping_does_not_require_primary_routes() -> None:
    acceptance = _acceptance_module()
    expected = {"svc:vonk-forge", "svc:hermes-api", "svc:hermes-dashboard"}
    status = _tailscale_status(hermes=True)
    status["Self"].pop("PrimaryRoutes")

    acceptance.assert_tailnet_service_mappings(
        status,
        expected_services=expected,
    )


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
        with pytest.raises(AcceptanceError):
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
        "Web": {
            "unexpected.acceptance.example.test:443": {
                "Handlers": {"/": {"Proxy": "http://unexpected:9999"}}
            }
        },
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
        with pytest.raises(AcceptanceError):
            acceptance.assert_tailnet_serve_status(
                json.dumps(document),
                hermes=True,
                tailnet_suffix="acceptance.example.test",
            )
    with pytest.raises(AcceptanceError):
        acceptance.assert_tailnet_serve_status(
            '{"Services":{},"Services":{}}',
            hermes=False,
            tailnet_suffix="acceptance.example.test",
        )


def test_tailnet_service_probe_uses_an_independent_host_client(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    compose = tmp_path / "compose"
    compose.write_text("#!/bin/sh\n")
    compose.chmod(0o755)
    monkeypatch.setenv("VONK_ACCEPTANCE_REFERENCE_COMPOSE", str(compose))
    compose_commands: list[list[str]] = []
    probe_commands: list[list[str]] = []

    responses = iter(
        (
            json.dumps(_tailscale_status(hermes=True)),
            json.dumps(_serve_status(hermes=True)),
            json.dumps(_serve_configuration(hermes=True)),
        )
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        compose_commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=next(responses), stderr=""
        )

    def probe(command: list[str], **_kwargs: object) -> bytes:
        probe_commands.append(command)
        return b"HTTP/1.1 200 OK\r\n\r\n"

    monkeypatch.setattr(acceptance, "run", run)
    monkeypatch.setattr(acceptance, "https_over_command", probe)

    acceptance.verify_tailscale_services(
        tmp_path,
        hermes=True,
        tailnet_suffix="acceptance.example.test",
        service_addresses={
            "svc:vonk-forge": "100.64.0.10",
            "svc:hermes-api": "100.64.0.11",
            "svc:hermes-dashboard": "100.64.0.12",
        },
        compose_command=["docker", "compose", "--project-name", "isolated"],
    )

    assert all(
        command[:4] == ["docker", "compose", "--project-name", "isolated"]
        for command in compose_commands
    )
    assert probe_commands == [
        acceptance._tailnet_tunnel("vonk-forge.acceptance.example.test", 443),
        acceptance._tailnet_tunnel("hermes-dashboard.acceptance.example.test", 443),
    ]

    local_status = _tailscale_status(hermes=True)
    local_status["Self"]["CapMap"] = {}  # type: ignore[index]
    local_status["Self"]["PrimaryRoutes"] = []  # type: ignore[index]
    responses = iter(
        (
            json.dumps(local_status),
            json.dumps(_serve_status(hermes=True)),
            json.dumps(_serve_configuration(hermes=True)),
        )
    )
    compose_commands.clear()
    probe_commands.clear()
    acceptance.verify_tailscale_services(
        tmp_path,
        hermes=True,
        tailnet_suffix="acceptance.example.test",
        service_addresses=None,
    )
    assert all(command[0] == str(compose) for command in compose_commands)
    assert probe_commands == []


def test_no_client_accepts_only_exact_pending_ephemeral_advertisement(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    compose = tmp_path / "compose"
    compose.write_text("#!/bin/sh\n")
    compose.chmod(0o755)
    monkeypatch.setenv("VONK_ACCEPTANCE_REFERENCE_COMPOSE", str(compose))
    local_status = _tailscale_status(hermes=False)
    local_status["Self"]["CapMap"] = {}  # type: ignore[index]
    local_status["Self"]["PrimaryRoutes"] = []  # type: ignore[index]

    responses = iter((json.dumps(local_status), "{}", '{"version":"0.0.1"}'))

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, stdout=next(responses), stderr=""
        )

    monkeypatch.setattr(acceptance, "run", run)
    acceptance.verify_tailscale_services(
        tmp_path,
        hermes=False,
        tailnet_suffix="acceptance.example.test",
        service_addresses=None,
    )

    responses = iter(
        (
            json.dumps(_tailscale_status(hermes=False)),
            "{}",
            '{"version":"0.0.1"}',
        )
    )
    with pytest.raises(AcceptanceError):
        acceptance.verify_tailscale_services(
            tmp_path,
            hermes=False,
            tailnet_suffix="acceptance.example.test",
            service_addresses={"svc:vonk-forge": "100.64.0.10"},
        )

    assert not acceptance.tailnet_serve_is_pending_ephemeral_advertisement(
        "{}", '{"version":"0.0.1","services":{}}'
    )


def test_wait_for_tailnet_services_polls_until_all_are_visible(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    expected = {"svc:vonk-forge-acceptance": "vonk-forge-acceptance.example.ts.net"}
    outputs = iter(
        (
            socket.gaierror(),
            [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("fd7a:115c:a1e0::1", 443, 0, 0),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("100.64.0.10", 443),
                ),
            ],
        )
    )

    def resolve(*_args: object, **_kwargs: object) -> object:
        result = next(outputs)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(acceptance.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(acceptance.time, "sleep", lambda _seconds: None)

    assert acceptance.wait_for_tailnet_services(tmp_path, expected) == {
        "svc:vonk-forge-acceptance": "100.64.0.10"
    }


def test_wait_for_tailnet_https_retries_with_service_hostname(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    calls: list[tuple[list[str], _RequestKwargs]] = []

    def request(command: list[str], **kwargs: Unpack[_RequestKwargs]) -> bytes:
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise AcceptanceError("HTTPS tunnel timed out")
        return b"HTTP/1.1 200 OK\r\n\r\n"

    monkeypatch.setattr(acceptance, "https_over_command", request)
    monkeypatch.setattr(acceptance.time, "sleep", lambda _seconds: None)

    acceptance.wait_for_tailnet_https(
        tmp_path,
        service="svc:vonk-forge-acceptance",
        hostname="vonk-forge-acceptance.example.ts.net",
        address="100.64.0.10",
        path="/healthz",
    )

    assert len(calls) == 2
    assert all(
        command
        == acceptance._tailnet_tunnel("vonk-forge-acceptance.example.ts.net", 443)
        and kwargs["server_hostname"] == "vonk-forge-acceptance.example.ts.net"
        and kwargs["timeout"] <= 10
        for command, kwargs in calls
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
        "step-ca/root-certificate": "root",
    }.items():
        target = secrets / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    calls: list[tuple[list[str], _RequestKwargs]] = []
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

    def request(command: list[str], **kwargs: Unpack[_RequestKwargs]) -> bytes:
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
            status = (
                "200 OK"
                if headers.get("Authorization") == "Bearer litellm-secret"
                else "401 Unauthorized"
            )
            assert int(status[:3]) in kwargs["accepted_statuses"]
            return f'HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\r\n{{"data":[]}}'.encode()
        raise AssertionError(path)

    monkeypatch.setattr(acceptance, "https_over_command", request)

    acceptance.verify_routed_service_behavior(
        tmp_path,
        nas_ip="192.0.2.20",
        control_hostname="vonk-forge.acceptance.example.test",
        registry_hostname="registry.acceptance.example.test",
    )

    assert all(
        command == acceptance._tailnet_tunnel("vonk-forge.acceptance.example.test", 443)
        for command, kwargs in calls
        if kwargs["server_hostname"] != "registry.acceptance.example.test"
    )
    requests = {kwargs["path"]: kwargs for _, kwargs in calls}
    models = requests["/v1/models"]
    assert models["headers"] == {"Authorization": "Bearer litellm-secret"}
    assert models["accepted_statuses"] == {200}
    registry = next(
        kwargs
        for _, kwargs in calls
        if kwargs["server_hostname"] == "registry.acceptance.example.test"
        and kwargs.get("client_certificate") is not None
    )
    assert registry["ca_file"] == secrets / "step-ca/root-certificate"
    assert registry["client_certificate"] == client_certificate


@pytest.mark.parametrize(
    "mutation", [None, "wrong_role", "floating_candidate", "floating_upstream"]
)
def test_candidate_overlay_checks_exact_roles_and_pinned_upstream(mutation):
    acceptance = _acceptance_module()
    api = "ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-x@sha256:" + "a" * 64
    worker = "ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-x@sha256:" + "b" * 64
    expected = {"control-api": {"image": api}, "control-worker": {"image": worker}}
    services = {
        **expected,
        "postgres": {
            "image": "postgres:18.6@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722"
        },
    }
    if mutation == "wrong_role":
        services["control-api"] = {"image": worker}
    elif mutation == "floating_candidate":
        services["control-api"] = {"image": "ghcr.io/carstvaartjes/vonk-forge-api:dev"}
    elif mutation == "floating_upstream":
        services["postgres"] = {"image": "postgres:latest"}
    if mutation:
        with pytest.raises(acceptance.AcceptanceError):
            acceptance.assert_candidate_image_graph(services, expected)
    else:
        acceptance.assert_candidate_image_graph(services, expected)


@pytest.mark.parametrize(
    ("outcome", "expected_error", "expected_reads"),
    [
        ("recovers", None, 2),
        ("never-completes", "fresh completed loop", 20),
        ("api-mismatch", "differs from its installed package source", 1),
        ("worker-mismatch", "differ from their own installed package", 2),
        ("unknown-provenance", "fresh completed loop", 1),
    ],
)
def test_controller_provenance_waits_only_for_missing_completed_worker_loop(
    tmp_path: Path, monkeypatch, outcome, expected_error, expected_reads
) -> None:
    acceptance = _acceptance_module()
    source = "a" * 40
    package = {
        "source_sha": source,
        "control_contract_sha256": "b" * 64,
        "worker_contract_sha256": "c" * 64,
    }
    monkeypatch.setenv("VONK_ACCEPTANCE_SOURCE_SHA", source)
    monkeypatch.setattr(acceptance, "reference_compose", lambda: ["compose"])
    elapsed = [0.0]
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        acceptance.time,
        "sleep",
        lambda duration: elapsed.__setitem__(0, elapsed[0] + duration),
    )
    reads = []

    def run(command, **kwargs):
        if "control-api" in command:
            reads.append(kwargs["timeout"])
            missing = len(reads) == 1 or outcome == "never-completes"
            api = dict(package)
            if outcome == "api-mismatch":
                api["source_sha"] = "d" * 40
            workers = None if missing else [dict(package)]
            issue = "worker-observation-unavailable" if missing else None
            if outcome == "unknown-provenance":
                workers = [dict(package, source_sha=None)]
                issue = "worker-provenance-unavailable"
            if outcome == "worker-mismatch" and workers:
                workers[0]["source_sha"] = "e" * 40
            document = {
                "package": package,
                "observation": {"api": api, "workers": workers, "worker_issue": issue},
            }
        else:
            document = package
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(document), stderr=""
        )

    monkeypatch.setattr(acceptance, "run", run)
    if expected_error:
        with pytest.raises(AcceptanceError):
            acceptance.verify_deployed_controller_identity(tmp_path)
    else:
        acceptance.verify_deployed_controller_identity(tmp_path)
    assert len(reads) == expected_reads
    assert reads == [20 - index for index in range(expected_reads)]
    assert elapsed[0] <= 20
