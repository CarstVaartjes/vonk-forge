from __future__ import annotations

import importlib.util
import json
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

    lifecycle._configure_acceptance_renewal(bundle, lifetime_seconds=90)

    ca = json.loads((bundle / "secrets/step-ca/ca.json").read_text())
    claims = ca["authority"]["provisioners"][0]["claims"]
    assert claims == {
        "defaultTLSCertDuration": "90s",
        "disableRenewal": True,
        "maxTLSCertDuration": "90s",
        "minTLSCertDuration": "90s",
    }
    compose = (bundle / "docker-compose.yaml").read_text()
    assert "VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS: '90'" in compose


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
