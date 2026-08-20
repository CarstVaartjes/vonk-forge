from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/install-release-publication"
DIGEST = "a" * 64
SOURCE_SHA = "b" * 40
NAS_PLATFORMS = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
)
SPARK_PLATFORMS = ("linux-amd64", "linux-arm64")


def _canonical(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


def _inputs(tmp_path: Path, *, status: str = "accepted") -> dict[str, object]:
    accepted = tmp_path / "accepted.json"
    _canonical(
        accepted,
        {
            "channel": "stable",
            "gates": {
                "agent_packages": True,
                "compose": True,
                "nas_payload": True,
                "setup_binaries": True,
                "source_authority": True,
            },
            "schema_version": 1,
            "source_sha": SOURCE_SHA,
            "status": status,
            "version": "1.2.3",
        },
    )
    nas: dict[str, Path] = {}
    for platform in NAS_PLATFORMS:
        path = tmp_path / f"vonk-nas-setup-{platform}"
        path.write_bytes(f"nas setup {platform}\n".encode())
        nas[platform] = path
    spark: dict[str, Path] = {}
    packages: dict[str, Path] = {}
    for platform in SPARK_PLATFORMS:
        path = tmp_path / f"vonk-spark-setup-{platform}"
        path.write_bytes(f"spark setup {platform}\n".encode())
        spark[platform] = path
        architecture = platform.removeprefix("linux-")
        package = tmp_path / f"vonk-forge-agent_1.2.3_{architecture}.deb"
        package.write_bytes(f"package {platform}\n".encode())
        packages[platform] = package
    payload = tmp_path / "payload.json"
    _canonical(payload, {"compose": "pinned", "schema_version": 1})
    return {
        "accepted": accepted,
        "nas": nas,
        "spark": spark,
        "packages": packages,
        "payload": payload,
    }


def _assemble_command(tmp_path: Path, inputs: dict[str, object]) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "assemble",
        "--channel",
        "stable",
        "--version",
        "1.2.3",
        "--source-sha",
        SOURCE_SHA,
        "--accepted-evidence",
        str(inputs["accepted"]),
        "--origin",
        "https://install.vonkforge.ai",
        "--api-image",
        f"ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3@sha256:{DIGEST}",
        "--worker-image",
        f"ghcr.io/carstvaartjes/vonk-forge-worker:v1.2.3@sha256:{DIGEST}",
        "--hermes-image",
        f"ghcr.io/carstvaartjes/vonk-forge-hermes:1.2.3@sha256:{DIGEST}",
        "--nas-payload",
        str(inputs["payload"]),
        "--output",
        str(tmp_path / "publication"),
    ]
    for platform, path in inputs["nas"].items():
        command.extend(("--nas-setup", f"{platform}={path}"))
    for platform, path in inputs["spark"].items():
        command.extend(("--spark-setup", f"{platform}={path}"))
    for platform, path in inputs["packages"].items():
        command.extend(("--agent-package", f"{platform}={path}"))
    return command


def _assemble(tmp_path: Path, inputs: dict[str, object]) -> Path:
    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / "publication"


def test_assemble_builds_complete_immutable_generation_and_final_pointer(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    plan = json.loads((publication / "publication-plan.json").read_text())

    assert plan["schema_version"] == 1
    assert plan["channel"] == "stable"
    phases = [entry["phase"] for entry in plan["objects"]]
    assert phases == sorted(
        phases, key={"immutable": 0, "endpoint": 1, "pointer": 2}.get
    )
    assert phases[-1] == "pointer"
    assert phases.count("pointer") == 1
    keys = {entry["key"] for entry in plan["objects"]}
    for platform in NAS_PLATFORMS:
        assert any(
            key.endswith(f"/nas/current/{platform}/vonk-nas-setup") for key in keys
        )
    for platform in SPARK_PLATFORMS:
        assert any(
            key.endswith(f"/spark/current/{platform}/vonk-spark-setup") for key in keys
        )
        assert any(
            key.endswith(f"/spark/current/{platform}/vonk-forge-agent.deb")
            for key in keys
        )
    assert {
        entry["key"] for entry in plan["objects"] if entry["phase"] == "endpoint"
    } == {
        "nas",
        "spark",
    }
    assert plan["objects"][-1]["key"] == "artifacts/stable/current.json"


def test_bootstraps_pin_final_digests_and_immutable_generation_urls(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())
    generation = plan["generation"]

    for kind in ("nas", "spark"):
        bootstrap = (publication / "objects" / kind).read_text()
        assert (
            f"https://install.vonkforge.ai/artifacts/stable/releases/{generation}"
            in bootstrap
        )
        assert "@[" not in bootstrap
    nas = (publication / "objects/nas").read_text()
    spark = (publication / "objects/spark").read_text()
    for path in inputs["nas"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in nas
    for path in inputs["spark"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in spark
    for path in inputs["packages"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in spark
    assert hashlib.sha256(inputs["payload"].read_bytes()).hexdigest() in nas


@pytest.mark.parametrize(
    "missing",
    ("nas-linux-amd64", "nas-darwin-arm64", "spark-linux-arm64", "package-linux-amd64"),
)
def test_assemble_rejects_incomplete_platform_matrix(
    tmp_path: Path, missing: str
) -> None:
    inputs = _inputs(tmp_path)
    kind, platform = missing.split("-", 1)
    collection = {
        "nas": inputs["nas"],
        "spark": inputs["spark"],
        "package": inputs["packages"],
    }[kind]
    collection.pop(platform)

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "platforms are incomplete" in result.stderr
    assert not (tmp_path / "publication").exists()


def test_assemble_refuses_unaccepted_evidence(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, status="rejected")

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "evidence is not accepted" in result.stderr
    assert not (tmp_path / "publication").exists()


@pytest.mark.parametrize("role", ("api", "worker", "hermes"))
def test_assemble_refuses_mutable_image_references(tmp_path: Path, role: str) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    option = f"--{role}-image"
    command[command.index(option) + 1] = (
        f"ghcr.io/carstvaartjes/vonk-forge-{role}:latest@sha256:{DIGEST}"
    )

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "image reference is mutable" in result.stderr


def test_publish_writes_pointer_after_immutable_objects_and_endpoints(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    destination = tmp_path / "public"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish",
            "--bundle",
            str(publication),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert operations[-1] == {
        "key": "artifacts/stable/current.json",
        "phase": "pointer",
    }
    assert all(item["phase"] == "immutable" for item in operations[:-3])
    assert [item["phase"] for item in operations[-3:]] == [
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = json.loads((destination / "artifacts/stable/current.json").read_text())
    assert pointer["generation"]
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()
