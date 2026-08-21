from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

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

    assert observed["command"] == [
        "/bin/sh",
        "-c",
        "curl -fsSL 'https://install.example/artifacts/release/bootstraps/spark' | sh",
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
    )

    lifecycle._configure_acceptance_renewal(bundle, lifetime_seconds=300)

    ca = json.loads((bundle / "secrets/step-ca/ca.json").read_text())
    claims = ca["authority"]["provisioners"][0]["claims"]
    assert claims == {
        "defaultTLSCertDuration": "300s",
        "disableRenewal": True,
        "maxTLSCertDuration": "300s",
        "minTLSCertDuration": "300s",
    }
    compose = (bundle / "docker-compose.yaml").read_text()
    assert "VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS: '300'" in compose


def test_synthetic_device_fixture_is_native_arm64_only() -> None:
    lifecycle = _module()

    raw, digest = lifecycle._synthetic_device_fixture("linux-arm64")

    document = json.loads(raw)
    assert document["kind"] == "nvidia.com/gpu"
    assert document["devices"] == [
        {
            "containerEdits": {"env": ["VONK_SYNTHETIC_CDI=1"]},
            "name": "all",
        }
    ]
    assert len(digest) == 64
    with pytest.raises(lifecycle.LifecycleError, match="ARM64"):
        lifecycle._synthetic_device_fixture("linux-amd64")


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
