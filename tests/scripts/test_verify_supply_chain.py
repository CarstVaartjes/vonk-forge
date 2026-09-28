import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-supply-chain"
INPUTS = (
    "Cargo.lock",
    "control/uv.lock",
    "control/web/package-lock.json",
    "control/packaging/public-contracts.lock",
    "control/Dockerfile",
    "agent_protocol/uv.lock",
    "agent_protocol/pyproject.toml",
    "deploy/compose/images.lock.json",
    "deploy/compose/compose.yaml",
    "deploy/compose/tailscale/compose.yaml",
    "deploy/compose/hermes-agent/compose.yaml",
    "deploy/compose/hermes-agent/Dockerfile",
    "deploy/compose/litellm/Dockerfile",
    "deploy/compose/trust/litellm-cosign.pub",
    "inventory/wheels/vonk_forge_public_contracts-0.1.0-py3-none-any.whl",
)
SUBPROCESS_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    for relative in INPUTS:
        source = ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (target / "deploy/compose/litellm").mkdir(parents=True, exist_ok=True)
    return target


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *arguments, "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=SUBPROCESS_ENV,
    )


def _result(run: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(run.stdout)


def _errors(run: subprocess.CompletedProcess[str]) -> list[str]:
    errors = _result(run)["errors"]
    assert isinstance(errors, list)
    return [str(error) for error in errors]


def test_verifier_generates_deterministic_lock_derived_evidence(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    output = tmp_path / "evidence"

    first = _run(repository, "--output-dir", str(output))
    first_manifest = (output / "manifest.json").read_bytes()
    first_sboms = {
        path.name: path.read_bytes() for path in sorted(output.glob("*.spdx.json"))
    }
    second = _run(repository, "--output-dir", str(output))

    assert first.returncode == second.returncode == 0
    assert _result(first)["ok"] is True
    assert first_manifest == (output / "manifest.json").read_bytes()
    assert first_sboms == {
        path.name: path.read_bytes() for path in sorted(output.glob("*.spdx.json"))
    }
    manifest = json.loads(first_manifest)
    assert (
        manifest["lockfiles"]["Cargo.lock"]
        == hashlib.sha256((repository / "Cargo.lock").read_bytes()).hexdigest()
    )
    assert (
        manifest["image_lock_sha256"]
        == hashlib.sha256(
            (repository / "deploy/compose/images.lock.json").read_bytes()
        ).hexdigest()
    )
    assert (
        manifest["public_contract_wheel_sha256"]
        == tomllib.loads(
            (repository / "control/packaging/public-contracts.lock").read_text()
        )["sha256"]
    )
    assert manifest["sboms"] == {
        f"inventory/sbom/{name}": hashlib.sha256(content).hexdigest()
        for name, content in first_sboms.items()
    }
    assert not (repository / "inventory/sbom").exists()


def test_local_verification_needs_no_generated_files_or_mutation(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)

    result = _run(repository)

    assert result.returncode == 0
    assert _result(result)["ok"] is True
    assert not (repository / "inventory/sbom").exists()
    assert not (
        repository / "inventory/wheels/vonk_agent_protocol-4.0.0-py3-none-any.whl"
    ).exists()


def test_lock_change_generates_updated_sbom_instead_of_stale_source_failure(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    output = tmp_path / "evidence"
    assert _run(repository, "--output-dir", str(output)).returncode == 0
    before = json.loads((output / "manifest.json").read_bytes())

    lock = repository / "control/web/package-lock.json"
    lock.write_text(lock.read_text() + "\n")
    changed = _run(repository, "--output-dir", str(output))
    after = json.loads((output / "manifest.json").read_bytes())

    assert changed.returncode == 0
    assert (
        after["lockfiles"]["control/web/package-lock.json"]
        != before["lockfiles"]["control/web/package-lock.json"]
    )
    assert (
        after["sboms"]["inventory/sbom/control-web.spdx.json"]
        != before["sboms"]["inventory/sbom/control-web.spdx.json"]
    )


@pytest.mark.parametrize(
    ("field", "replacement"), (("revision", "0" * 40), ("sha256", "0" * 64))
)
def test_reviewed_third_party_contract_wheel_pin_fails_closed(
    tmp_path: Path, field: str, replacement: str
) -> None:
    repository = _copy(tmp_path)
    lock = repository / "control/packaging/public-contracts.lock"
    source = lock.read_text()
    changed, count = re.subn(
        rf"(?m)^{field} = \"[0-9a-f]+\"$",
        f'{field} = "{replacement}"',
        source,
        count=1,
    )
    assert count == 1
    lock.write_text(changed)

    result = _run(repository)

    assert result.returncode != 0
    assert "public contract" in " ".join(_errors(result))


@pytest.mark.parametrize("image", ("caddy", "postgres", "step-ca", "tailscale"))
def test_image_lock_rejects_floating_runtime_references(
    tmp_path: Path, image: str
) -> None:
    repository = _copy(tmp_path)
    lock_path = repository / "deploy/compose/images.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["images"][image] = f"example/{image}:latest"
    lock_path.write_text(json.dumps(lock))

    result = _run(repository)

    assert result.returncode != 0
    assert f"{image} image uses a floating tag" in " ".join(_errors(result))


@pytest.mark.parametrize("name", ("hermes", "litellm", "node", "python"))
def test_image_lock_rejects_floating_build_bases(tmp_path: Path, name: str) -> None:
    repository = _copy(tmp_path)
    lock_path = repository / "deploy/compose/images.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["build_bases"][name] = f"example/{name}:latest"
    lock_path.write_text(json.dumps(lock))

    result = _run(repository)

    assert result.returncode != 0
    assert f"{name} image uses a floating tag" in " ".join(_errors(result))


def test_verifier_rejects_protocol_version_drift(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    project = repository / "agent_protocol/pyproject.toml"
    project.write_text(
        project.read_text().replace('version = "4.0.0"', 'version = "4.0.1"', 1)
    )

    result = _run(repository)

    assert result.returncode != 0
    assert "version" in " ".join(_errors(result))


@pytest.mark.parametrize("value", (None, [], {}))
def test_malformed_runtime_image_lock_values_fail_closed(
    tmp_path: Path, value: object
) -> None:
    repository = _copy(tmp_path)
    lock_path = repository / "deploy/compose/images.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["images"]["caddy"] = value
    lock_path.write_text(json.dumps(lock))

    result = _run(repository)

    assert result.returncode != 0
    assert "caddy image is not pinned by version" in " ".join(_errors(result))


def test_generated_protocol_spdx_binds_built_wheel_checksum(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/vonk_agent_protocol-4.0.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(b"built wheel bytes")
    output = tmp_path / "evidence"

    result = _run(repository, "--output-dir", str(output))

    assert result.returncode == 0
    document = json.loads((output / "agent-protocol.spdx.json").read_bytes())
    protocol = next(
        item for item in document["packages"] if item["name"] == "vonk-agent-protocol"
    )
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert protocol["checksums"] == [{"algorithm": "SHA256", "checksumValue": checksum}]
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["protocol_wheel_sha256"] == checksum


def test_dockerfile_must_copy_and_install_the_protocol_wheel(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "control/Dockerfile"
    source = dockerfile.read_text()
    dockerfile.write_text(
        source.replace(
            "/wheels/vonk_agent_protocol-4.0.0-py3-none-any.whl",
            "/wheels/missing.whl",
            1,
        )
    )

    result = _run(repository)

    assert result.returncode != 0
    assert "standalone protocol wheel" in " ".join(_errors(result))
