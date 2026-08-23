from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ENTRY_POINT = Path(__file__).parent / "acceptance/test_spark_lifecycle.py"


def _module():
    specification = importlib.util.spec_from_file_location(
        "spark_lifecycle_runner", ENTRY_POINT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_literal_spark_bootstrap_keeps_pairing_token_only_in_tty_answers(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    observed: dict[str, object] = {}

    def interactive(command, **kwargs):
        observed.update(command=command, **kwargs)
        return "installed"

    token = "single-use-pairing-secret"
    environment = {
        "PATH": "/usr/bin:/bin",
        "VONK_INSTALL_BASE_URL": "https://install.example/artifacts/release",
        "VONK_INSTALL_RELEASE_MANIFEST": "/objects/release.json",
        "VONK_INSTALL_RELEASE_SIGNATURE": "/objects/release.sig",
    }
    lifecycle._run_spark_bootstrap(
        "https://install.example/artifacts/release/bootstraps/spark",
        cwd=tmp_path,
        environment=environment,
        enrollment_url="https://enroll.spark.localhost:8443",
        ca_sha256="a" * 64,
        pairing_token=token,
        interactive=interactive,
    )

    command = observed["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert "curl --fail --location --silent --show-error" in command[2]
    assert "--retry 30 --retry-all-errors" in command[2]
    assert "| sh" not in command[2]
    assert command[3:] == [
        "vonk-bootstrap",
        "https://install.example/artifacts/release/bootstraps/spark",
    ]
    assert observed["responses"] == [
        ("Enrollment URL: ", "https://enroll.spark.localhost:8443"),
        ("Controller CA SHA-256: ", "a" * 64),
        ("Pairing token: ", token),
    ]
    assert observed["forbidden_values"] == [token]
    assert token not in repr(observed["command"])
    assert token not in repr(observed["environment"])


def test_acceptance_controller_configuration_is_short_lived_and_generation_bound(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    bundle = tmp_path / "bundle"
    (bundle / "secrets/step-ca").mkdir(parents=True)
    (bundle / "secrets/step-ca/ca.json").write_text(
        json.dumps(
            {
                "authority": {
                    "provisioners": [
                        {
                            "name": "vonk-forge-agent",
                            "claims": {
                                "minTLSCertDuration": "24h",
                                "maxTLSCertDuration": "24h",
                                "defaultTLSCertDuration": "24h",
                                "disableRenewal": True,
                                "disableSmallstepExtensions": True,
                            },
                        }
                    ]
                }
            }
        )
    )
    (bundle / "docker-compose.yaml").write_text(
        "services:\n  control-api:\n    image: ghcr.io/vonk/api@sha256:"
        + "a" * 64
        + "\n    environment:\n      VONK_DEPLOYMENT_MODE: production\n"
        + "  caddy:\n    image: caddy:acceptance\n    networks: [ingress]\n    ports:\n"
        + "      - target: 8443\n        published: 8443\n"
        + "networks:\n  ingress: {}\n  cluster-egress: {}\n"
    )

    lifecycle._configure_acceptance_renewal(bundle, lifetime_seconds=300)

    ca = json.loads((bundle / "secrets/step-ca/ca.json").read_text())
    claims = ca["authority"]["provisioners"][0]["claims"]
    assert claims == {
        "defaultTLSCertDuration": "300s",
        "disableRenewal": True,
        "disableSmallstepExtensions": True,
        "maxTLSCertDuration": "300s",
        "minTLSCertDuration": "300s",
    }
    compose = (bundle / "docker-compose.yaml").read_text()
    assert "VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS: '300'" in compose
    assert "127.0.0.1::8080" in compose
    assert "- cluster-egress" in compose


def test_synthetic_device_fixture_supports_each_native_package_runner() -> None:
    lifecycle = _module()

    arm64_raw, arm64_digest = lifecycle._synthetic_device_fixture("linux-arm64")
    amd64_raw, amd64_digest = lifecycle._synthetic_device_fixture("linux-amd64")

    document = json.loads(arm64_raw)
    assert document["kind"] == "nvidia.com/gpu"
    assert document["devices"] == [
        {
            "containerEdits": {"env": ["VONK_SYNTHETIC_CDI=1"]},
            "name": "all",
        }
    ]
    assert amd64_raw == arm64_raw
    assert len(arm64_digest) == 64
    assert amd64_digest == arm64_digest
    with pytest.raises(lifecycle.LifecycleError, match="platform"):
        lifecycle._synthetic_device_fixture("linux-riscv64")


def test_synthetic_firewall_is_not_materialized_before_baseline_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    monkeypatch.setattr(lifecycle, "_agent_package_installed", lambda: False)

    with pytest.raises(lifecycle.LifecycleError, match="must follow baseline"):
        run._materialize_synthetic_firewall()


def test_cleanup_targets_only_the_exact_compose_project_and_its_volumes(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    root = tmp_path / "run"
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    observed: list[tuple[list[str], Path, int]] = []
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = bundle
    run.project = "vonk-spark-42-arm64"
    run.temporary_root = root
    run.synthetic_paths = []
    run._run_command = lambda command, *, cwd, timeout=300: observed.append(
        (command, cwd, timeout)
    )

    run._cleanup()

    assert observed == [
        (
            [
                "sudo",
                "/usr/bin/rm",
                "-rf",
                "--",
                "/etc/vonk-forge-agent",
                "/var/lib/vonk-forge-agent",
            ],
            Path("/"),
            60,
        ),
        (
            [
                "docker",
                "compose",
                "--project-name",
                "vonk-spark-42-arm64",
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "30",
            ],
            bundle,
            120,
        )
    ]


def test_local_browser_controller_uses_only_the_loopback_publication(monkeypatch) -> None:
    lifecycle = _module()
    observed: dict[str, object] = {}

    class Response:
        status = 200

        @staticmethod
        def getheaders():
            return [("Content-Type", "application/json")]

        @staticmethod
        def read(limit):
            observed["limit"] = limit
            return b"{}"

    class Connection:
        def __init__(self, host, port, *, timeout):
            observed.update(host=host, port=port, timeout=timeout)

        def request(self, method, path, *, body, headers):
            observed.update(method=method, path=path, body=body, headers=headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            observed["closed"] = True

    monkeypatch.setattr(lifecycle.http.client, "HTTPConnection", Connection)
    boundary = lifecycle.LocalBrowserController(
        hostname="vonk-forge.acceptance.example.test",
        port=49152,
    )

    assert boundary.raw_request("GET", "/healthz", None, {}, 5) == (
        200,
        {"content-type": ["application/json"]},
        b"{}",
    )
    assert observed == {
        "host": "127.0.0.1",
        "port": 49152,
        "timeout": 5,
        "method": "GET",
        "path": "/healthz",
        "body": None,
        "headers": {"Host": "vonk-forge.acceptance.example.test"},
        "limit": lifecycle.MAXIMUM_RESPONSE_BYTES + 1,
        "closed": True,
    }


def test_local_browser_port_is_discovered_from_the_isolated_project(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-amd64"
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout=300):
        observed.append(argv)
        assert cwd == tmp_path
        return subprocess.CompletedProcess(argv, 0, stdout="127.0.0.1:49152\n")

    run._run_command = command

    assert run._local_browser_port() == 49152
    assert observed == [
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-amd64",
            "port",
            "caddy",
            "8080",
        ]
    ]


def test_enrollment_grant_requires_the_installer_route_metadata() -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control_hostname = "vonk-forge-acceptance.tailnet.example"
    grant = {
        "ca_fingerprint": "a" * 64,
        "controller_address": "127.0.0.1",
        "controller_endpoint": "https://agents.spark.localhost:8443",
        "enrollment_endpoint": "https://enroll.spark.localhost:8443",
        "expires_at": "2026-08-22T20:00:00Z",
        "id": "11111111-1111-1111-1111-111111111111",
        "purpose": "new-node",
        "service_hostnames": [
            "vonk-forge-acceptance.tailnet.example",
            "enroll.spark.localhost",
            "agents.spark.localhost",
            "registry.spark.localhost",
        ],
        "token": "t" * 43,
    }

    class Control:
        @staticmethod
        def request(method, path, body):
            assert (method, path, body) == (
                "POST",
                "/api/v1/agents/enrollments/grants",
                {"ttl_seconds": 600},
            )
            return 201, dict(grant)

    run.control = Control()

    assert run._create_grant() == (
        grant["id"],
        grant["enrollment_endpoint"],
        grant["ca_fingerprint"],
        grant["token"],
    )


def test_installer_environment_routes_spark_bootstrap_to_acceptance_controller(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    candidate = tmp_path / "candidate/release.json"
    baseline = tmp_path / "baseline/release.json"
    for release in (candidate, baseline):
        release.parent.mkdir(parents=True)
        release.write_text("{}")
        (release.parent / "release.sig").write_text("signature")
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.temporary_root = tmp_path
    run.origin = "https://install.example"
    run.arguments = SimpleNamespace(
        baseline_release=baseline,
        candidate_release=candidate,
        channel="dev",
        generation="a" * 64,
    )

    candidate_environment = run._installer_environment(baseline=False)
    baseline_environment = run._installer_environment(baseline=True)

    assert candidate_environment["VONK_CONTROLLER_ADDRESS"] == "127.0.0.1"
    assert baseline_environment["VONK_CONTROLLER_ADDRESS"] == "127.0.0.1"
    assert candidate_environment["VONK_INSTALL_BASE_URL"].endswith("/" + "a" * 64)
    assert baseline_environment["VONK_INSTALL_BASE_URL"].endswith(
        "/" + "a" * 64 + "/acceptance-baseline"
    )


def test_controller_startup_diagnostics_are_bounded_and_redact_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
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
                    "Health": "",
                    "Service": "tailscale-gateway",
                    "State": "restarting",
                },
            ]
        ),
        stderr="",
    )
    logs = subprocess.CompletedProcess(
        [],
        0,
        stdout=f"discarded diagnostic beginning{'x' * 9_000}\nauthentication failed for {secret}\n",
        stderr="",
    )
    outputs = iter((status, logs))
    run._diagnostic_command = lambda _command: next(outputs)

    diagnostics = run._controller_startup_diagnostics()

    assert secret not in diagnostics
    assert "discarded diagnostic beginning" not in diagnostics
    assert len(diagnostics) < 8_500
    assert "authentication failed for <redacted>" in diagnostics
    assert "postgres=running/healthy/exit-0" in diagnostics
    assert "tailscale-gateway=restarting/none/exit-1" in diagnostics


def test_installer_failure_diagnostics_are_bounded_and_redact_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    error = lifecycle.AcceptanceError(
        f"discarded diagnostic beginning{'x' * 9_000}\n"
        f"setup command failed for {secret}\n"
    )

    failure = run._installation_failure("baseline Spark installation", error)
    rendered = str(failure)

    assert secret not in rendered
    assert "discarded diagnostic beginning" not in rendered
    assert len(rendered) < 8_500
    assert "baseline Spark installation failed" in rendered
    assert "setup command failed for <redacted>" in rendered


def test_installer_failure_includes_redacted_controller_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    observed: list[list[str]] = []

    def diagnostics(command):
        observed.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"control enrollment failed for {secret}\n",
            stderr="",
        )

    run._diagnostic_command = diagnostics

    failure = run._installation_failure(
        "baseline Spark installation", lifecycle.AcceptanceError("Error: Status(500)")
    )

    assert secret not in str(failure)
    assert "Error: Status(500)" in str(failure)
    assert "control enrollment failed for <redacted>" in str(failure)
    assert observed == [
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-arm64",
            "logs",
            "--no-color",
            "--tail",
            "120",
            "control-api",
            "step-ca",
            "caddy",
        ]
    ]


def test_installer_error_survives_bounded_controller_diagnostics(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-amd64"
    run._diagnostic_command = lambda command: subprocess.CompletedProcess(
        command,
        0,
        stdout="controller-log\n" * 1_000,
        stderr="",
    )

    failure = run._installation_failure(
        "baseline Spark installation", lifecycle.AcceptanceError("Error: Certificate")
    )

    rendered = str(failure)
    assert len(rendered) < 8_500
    assert "Error: Certificate" in rendered


def test_direct_health_and_protected_identity_hash_are_observed_from_native_binary() -> (
    None
):
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run._self_test = lambda: {"self_test_passed": True}
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout):
        observed.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{'a' * 64}  {lifecycle.SPARK_CONFIG}\n",
            stderr="",
        )

    run._run_command = command

    assert run._direct_agent_health() == {
        "healthy": True,
        "implementation": "rust",
        "transport": "direct",
    }
    assert run._hash_path(lifecycle.SPARK_CONFIG) == "a" * 64
    assert observed == [
        [
            "sudo",
            "/usr/bin/sha256sum",
            "--",
            "/etc/vonk-forge-agent/agent.toml",
        ]
    ]
    with pytest.raises(lifecycle.LifecycleError, match="path"):
        run._hash_path(Path("/tmp/not-installation-identity"))


def test_renewal_requires_new_active_serial_and_real_old_identity_rejection() -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    node_id = "spk_" + "1" * 32
    serial_before = str(int("1234567890abcdef", 16))
    serial_after = str(int("abcdef1234567890", 16))
    run.graph = {"baseline_version": "1.2.3~acceptance.1+g" + "a" * 12}
    run._psql = lambda _query: [[serial_after, "revoked", "1"]]
    run._wait_for_agent_identity = lambda **_kwargs: {
        "node_id": node_id,
        "serial": serial_after,
    }
    run._old_certificate_rejected = lambda: True

    observed = run._observe_renewal(node_id, serial_before)

    assert observed == {
        "node_id": node_id,
        "proof": {
            "certificate_serial_after": "abcdef1234567890",
            "certificate_serial_before": "1234567890abcdef",
            "old_certificate_rejection": {
                "durably_recorded": True,
                "rejected": True,
                "serial": "1234567890abcdef",
            },
        },
    }
