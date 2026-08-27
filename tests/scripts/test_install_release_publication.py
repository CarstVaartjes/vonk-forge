from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

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
ACCEPTANCE_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
    "compose_hermes",
    "nas_site_secret_preservation",
    "nas_workstation",
    "spark_amd64",
    "spark_arm64",
    "spark_job",
    "spark_pairing",
    "spark_renewal",
}
AMD64_SPARK_GATES = {"spark_amd64", "spark_pairing"}
ARM64_SPARK_GATES = {"spark_arm64", "spark_job", "spark_renewal"}
AMD64_PHASES = [
    "publication-graph-verified",
    "controller-ready",
    "candidate-installed",
    "paired",
    "direct-rust-agent-healthy",
]
ARM64_PHASES = [
    "publication-graph-verified",
    "controller-ready",
    "candidate-installed",
    "paired",
    "synthetic-device-ready",
    "canary-completed",
    "identity-renewed",
    "direct-rust-agent-healthy",
]
CANARY_STATES = [
    "inventory-ready",
    "recipe-resolved",
    "source-verified",
    "image-built",
    "image-distributed",
    "installed",
    "running",
    "route-published",
    "inference-ok",
    "stopped",
    "route-withdrawn",
    "uninstalled",
]


def _canonical(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


def _agent_package(tmp_path: Path, platform: str, version: str) -> Path:
    architecture = platform.removeprefix("linux-")
    root = tmp_path / f"package-root-{version}-{architecture}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        "Package: vonk-forge-agent\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: test <test@example.test>\n"
        "Description: test\n",
        encoding="utf-8",
    )
    package = tmp_path / f"vonk-forge-agent_{version}_{architecture}.deb"
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--build", "--root-owner-group", root, package],
        check=True,
        capture_output=True,
    )
    Path(f"{package}.host.sig").write_text("e" * 128 + "\n")
    _canonical(
        package.with_suffix(".provenance.json"),
        {
            "predicate": {
                "buildDefinition": {
                    "externalParameters": {"build_digest": "sha256:" + "c" * 64}
                }
            },
            "subject": [
                {"digest": {"sha256": "d" * 64}, "name": "vonk-agent"}
            ],
        },
    )
    return package


def _inputs(
    tmp_path: Path,
    *,
    version: str = "1.2.3",
    channel: str = "stable",
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    signing_key = tmp_path / "installer-signing-key.pem"
    signing_public_key = tmp_path / "installer-signing-public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            signing_key,
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(signing_key, 0o600)
    subprocess.run(
        ["openssl", "pkey", "-in", signing_key, "-pubout", "-out", signing_public_key],
        check=True,
        capture_output=True,
    )
    nas: dict[str, Path] = {}
    for platform in NAS_PLATFORMS:
        path = tmp_path / f"vonk-nas-setup-{platform}"
        path.write_bytes(f"nas setup {platform}\n".encode())
        nas[platform] = path
    spark: dict[str, Path] = {}
    packages: dict[str, Path] = {}
    baseline_version = "0.0.0~acceptance.1+g" + SOURCE_SHA[:12]
    baseline_packages: dict[str, Path] = {}
    for platform in SPARK_PLATFORMS:
        path = tmp_path / f"vonk-spark-setup-{platform}"
        path.write_bytes(f"spark setup {platform}\n".encode())
        spark[platform] = path
        packages[platform] = _agent_package(tmp_path, platform, version)
        baseline_packages[platform] = _agent_package(
            tmp_path, platform, baseline_version
        )
    payload = tmp_path / "payload.json"
    _canonical(payload, {"compose": "pinned", "schema_version": 1})
    return {
        "nas": nas,
        "spark": spark,
        "packages": packages,
        "baseline_packages": baseline_packages,
        "baseline_version": baseline_version,
        "payload": payload,
        "signing_key": signing_key,
        "signing_public_key": signing_public_key,
        "version": version,
        "channel": channel,
    }


def _assemble_command(tmp_path: Path, inputs: dict[str, object]) -> list[str]:
    version = str(inputs["version"])
    channel = str(inputs["channel"])
    image_tag = f"v{version}" if channel == "stable" else f"dev-sha-{SOURCE_SHA}"
    hermes_tag = version if channel == "stable" else f"dev-sha-{SOURCE_SHA}"
    command = [
        sys.executable,
        str(SCRIPT),
        "assemble",
        "--channel",
        channel,
        "--version",
        version,
        "--source-sha",
        SOURCE_SHA,
        "--origin",
        "https://install.vonkforge.ai",
        "--expires-at",
        str(int(time.time()) + 7 * 24 * 60 * 60),
        "--signing-key",
        str(inputs["signing_key"]),
        "--signing-public-key",
        str(inputs["signing_public_key"]),
        "--api-image",
        f"ghcr.io/carstvaartjes/vonk-forge-api:{image_tag}@sha256:{DIGEST}",
        "--worker-image",
        f"ghcr.io/carstvaartjes/vonk-forge-worker:{image_tag}@sha256:{DIGEST}",
        "--hermes-image",
        f"ghcr.io/carstvaartjes/vonk-forge-hermes:{hermes_tag}@sha256:{DIGEST}",
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
    for platform, path in inputs["baseline_packages"].items():
        command.extend(("--agent-baseline-package", f"{platform}={path}"))
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


def _acceptance_receipt(
    tmp_path: Path,
    publication: Path,
    *,
    generation: str | None = None,
    status: str = "accepted",
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = json.loads((publication / "publication-plan.json").read_text())
    private_key = tmp_path / "acceptance-private.pem"
    public_key = tmp_path / "acceptance-public.pem"
    receipt = tmp_path / "acceptance.json"
    signature = tmp_path / "acceptance.sig"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            private_key,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key],
        check=True,
        capture_output=True,
    )
    _canonical(
        receipt,
        {
            "channel": plan["channel"],
            "gates": {name: True for name in sorted(ACCEPTANCE_GATES)},
            "generation": generation or plan["generation"],
            "issued_at": int(time.time()),
            "run_id": 123456,
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "status": status,
            "version": plan["version"],
        },
    )
    raw_signature = tmp_path / "acceptance.raw.sig"
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            private_key,
            "-out",
            raw_signature,
            receipt,
        ],
        check=True,
        capture_output=True,
    )
    signature.write_bytes(base64.b64encode(raw_signature.read_bytes()) + b"\n")
    return receipt, signature, public_key


def _publish_candidate(publication: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
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


def _fake_rclone_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    executable_root = tmp_path / "bin"
    executable_root.mkdir()
    object_root = tmp_path / "r2"
    object_root.mkdir()
    executable = executable_root / "rclone"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

command = sys.argv[1]
arguments = sys.argv[2:]
object_root = Path(os.environ["FAKE_RCLONE_ROOT"])

def object_path(value: str) -> Path:
    remote_path = value.split(":", 1)[1]
    key = remote_path.split("/", 1)[1]
    return object_root / key

if command == "lsjson":
    path = object_path(arguments[0])
    if not path.is_file():
        # Some object-store backends represent a missing exact object as a
        # successful stat with a non-object JSON payload.
        print("null")
        raise SystemExit(0)
    print(json.dumps({"IsDir": False, "Path": path.name, "Size": path.stat().st_size}))
elif command == "cat":
    path = object_path(arguments[0])
    # rclone cat succeeds with no output when its exact source object is absent.
    if path.is_file():
        sys.stdout.buffer.write(path.read_bytes())
elif command == "copyto":
    source = Path(arguments[-2])
    destination = object_path(arguments[-1])
    if "--immutable" in arguments and destination.exists():
        raise SystemExit(9)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
else:
    raise SystemExit(64)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{executable_root}:{environment['PATH']}"
    environment["FAKE_RCLONE_ROOT"] = str(object_root)
    return environment, object_root


def _gate_report(
    path: Path, publication: Path, gates: set[str], *, run_id: int = 123456
) -> Path:
    plan = json.loads((publication / "publication-plan.json").read_text())
    _canonical(
        path,
        {
            "channel": plan["channel"],
            "gates": sorted(gates),
            "generation": plan["generation"],
            "run_id": run_id,
            "schema_version": 1,
            "source_sha": SOURCE_SHA,
            "status": "passed",
            "version": plan["version"],
        },
    )
    return path


def _actual_publication_graph(
    publication: Path, platform: str
) -> dict[str, object]:
    plan = json.loads((publication / "publication-plan.json").read_text())
    object_root = publication / "objects"
    release_root = (
        object_root
        / f"artifacts/{plan['channel']}/releases/{plan['generation']}"
    )
    candidate = json.loads((release_root / "release.json").read_text())
    baseline = json.loads(
        (release_root / "acceptance-baseline/release.json").read_text()
    )
    packages: dict[str, dict[str, str]] = {}
    for native_platform in SPARK_PLATFORMS:
        candidate_record = candidate["artifacts"][
            f"agent-package-{native_platform}"
        ]
        baseline_record = baseline["artifacts"][
            f"agent-package-{native_platform}"
        ]
        candidate_bytes = (object_root / candidate_record["path"]).read_bytes()
        baseline_bytes = (object_root / baseline_record["path"]).read_bytes()
        packages[native_platform] = {
            "baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        }
    return {
        "baseline_package_sha256": packages[platform]["baseline_sha256"],
        "baseline_version": baseline["version"],
        "candidate_package_sha256": packages[platform]["candidate_sha256"],
        "candidate_version": candidate["version"],
        "channel": candidate["channel"],
        "generation": candidate["generation"],
        "images_sha256": hashlib.sha256(
            json.dumps(
                candidate["images"], sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        ).hexdigest(),
        "packages": packages,
        "platform": platform,
        "schema_version": 1,
        "source_sha": candidate["source_sha"],
        "verified_platforms": ["linux-amd64", "linux-arm64"],
    }


def _spark_gate_report(
    path: Path,
    publication: Path,
    gates: set[str],
    platform: str,
    *,
    run_id: int = 123456,
) -> Path:
    plan = json.loads((publication / "publication-plan.json").read_text())
    graph = _actual_publication_graph(publication, platform)
    node_id = "spk_0123456789abcdef0123456789abcdef"
    common_proof: dict[str, object] = {
        "controller_generation": plan["generation"],
        "direct_agent_health": {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        },
        "pairing_grant_use_count": 1,
        "publication_graph": graph,
    }
    if platform == "linux-amd64":
        phases = AMD64_PHASES
        proof = common_proof | {
            "installation": {
                "architecture": "amd64",
                "package_sha256": graph["packages"][platform]["candidate_sha256"],
                "version": plan["version"],
            },
            "node_id": node_id,
        }
    else:
        phases = ARM64_PHASES
        serial = "0123456789abcdef"
        proof = common_proof | {
            "canary": {
                "completed_states": CANARY_STATES,
                "deterministic_response_sha256": "5" * 64,
            },
            "installation": {
                "architecture": "arm64",
                "identity": {
                    "binary_sha256": "9" * 64,
                    "build_sha256": "a" * 64,
                    "package_sha256": graph["packages"][platform]["candidate_sha256"],
                    "version": plan["version"],
                },
            },
            "node_id_after_renewal": node_id,
            "node_id_before_renewal": node_id,
            "renewal": {
                "certificate_serial_after": "fedcba9876543210",
                "certificate_serial_before": serial,
                "old_certificate_rejection": {
                    "durably_recorded": True,
                    "rejected": True,
                    "serial": serial,
                },
            },
            "synthetic_device": {
                "architecture": platform,
                "cdi_name": "nvidia.com/gpu=all",
                "fixture_sha256": "e" * 64,
                "physical_gpu": False,
                "provenance": "ci-only-synthetic-cdi",
                "synthetic": True,
            },
        }
    _canonical(
        path,
        {
            "channel": plan["channel"],
            "gates": sorted(gates),
            "generation": plan["generation"],
            "lifecycle": {"completed_phases": phases, "proof": proof},
            "platform": platform,
            "run_id": run_id,
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "status": "passed",
            "version": plan["version"],
        },
    )
    return path


def _accept_command(
    publication: Path,
    output_root: Path,
    reports: list[Path],
    *,
    candidate_release: Path | None = None,
    baseline_release: Path | None = None,
    object_root: Path | None = None,
) -> list[str]:
    plan = json.loads((publication / "publication-plan.json").read_text())
    release_root = (
        publication
        / f"objects/artifacts/{plan['channel']}/releases/{plan['generation']}"
    )
    inputs = _inputs(output_root / "keys")
    command = [
        sys.executable,
        str(SCRIPT),
        "accept",
        "--channel",
        plan["channel"],
        "--version",
        plan["version"],
        "--source-sha",
        SOURCE_SHA,
        "--generation",
        plan["generation"],
        "--candidate-release",
        str(candidate_release or release_root / "release.json"),
        "--baseline-release",
        str(baseline_release or release_root / "acceptance-baseline/release.json"),
        "--object-root",
        str(object_root or publication / "objects"),
        "--run-id",
        "123456",
        "--signing-key",
        str(inputs["signing_key"]),
        "--signing-public-key",
        str(inputs["signing_public_key"]),
        "--output",
        str(output_root / "acceptance.json"),
        "--signature-output",
        str(output_root / "acceptance.sig"),
    ]
    for report in reports:
        command.extend(("--gate-report", str(report)))
    return command


def test_acceptance_authority_refuses_incomplete_behavioral_gate_reports(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report = _gate_report(
        tmp_path / "nas-report.json", publication, {"nas_workstation"}
    )

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", [report]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "behavioral acceptance gates are incomplete" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_acceptance_authority_signs_only_the_complete_exact_generation(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report_root = tmp_path / "reports"
    report_root.mkdir()
    reports = [
        _gate_report(
            report_root / "nas.json",
            publication,
            {
                "compose_compatibility_lower",
                "compose_compatibility_ugreen",
                "compose_default",
                "compose_hermes",
                "nas_site_secret_preservation",
                "nas_workstation",
            },
        ),
        _spark_gate_report(
            report_root / "spark-amd64.json",
            publication,
            AMD64_SPARK_GATES,
            "linux-amd64",
        ),
        _spark_gate_report(
            report_root / "spark-arm64.json",
            publication,
            ARM64_SPARK_GATES,
            "linux-arm64",
        ),
    ]

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", reports),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = tmp_path / "acceptance/acceptance.json"
    signature = tmp_path / "acceptance/acceptance.sig"
    document = json.loads(receipt.read_text())
    assert set(document["gates"]) == ACCEPTANCE_GATES
    assert all(document["gates"].values())
    signature_raw = signature.read_bytes()
    assert signature_raw.endswith(b"\n")
    assert signature_raw.count(b"\n") == 1
    (tmp_path / "acceptance/raw.sig").write_bytes(
        base64.b64decode(signature_raw.strip(), validate=True)
    )
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            tmp_path / "acceptance/keys/installer-signing-public.pem",
            "-signature",
            tmp_path / "acceptance/raw.sig",
            receipt,
        ],
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr

    destination = tmp_path / "promoted"
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    promoted = _promote(
        publication,
        destination,
        receipt,
        signature,
        tmp_path / "acceptance/keys/installer-signing-public.pem",
    )
    assert promoted.returncode == 0, promoted.stderr


def test_acceptance_authority_rejects_internally_consistent_invented_graph(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report_root = tmp_path / "reports"
    report_root.mkdir()
    nas = _gate_report(
        report_root / "nas.json",
        publication,
        ACCEPTANCE_GATES - AMD64_SPARK_GATES - ARM64_SPARK_GATES,
    )
    amd64 = _spark_gate_report(
        report_root / "amd64.json",
        publication,
        AMD64_SPARK_GATES,
        "linux-amd64",
    )
    arm64 = _spark_gate_report(
        report_root / "arm64.json",
        publication,
        ARM64_SPARK_GATES,
        "linux-arm64",
    )
    invented_digest = "f" * 64
    for report_path in (amd64, arm64):
        report = json.loads(report_path.read_text())
        graph = report["lifecycle"]["proof"]["publication_graph"]
        graph["packages"]["linux-arm64"]["candidate_sha256"] = invented_digest
        if report["platform"] == "linux-arm64":
            graph["candidate_package_sha256"] = invented_digest
            report["lifecycle"]["proof"]["installation"]["identity"][
                "package_sha256"
            ] = invented_digest
        _canonical(report_path, report)

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", [nas, amd64, arm64]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "publication graph" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def _complete_gate_reports(
    report_root: Path, publication: Path
) -> list[Path]:
    report_root.mkdir()
    return [
        _gate_report(
            report_root / "nas.json",
            publication,
            ACCEPTANCE_GATES - AMD64_SPARK_GATES - ARM64_SPARK_GATES,
        ),
        _spark_gate_report(
            report_root / "amd64.json",
            publication,
            AMD64_SPARK_GATES,
            "linux-amd64",
        ),
        _spark_gate_report(
            report_root / "arm64.json",
            publication,
            ARM64_SPARK_GATES,
            "linux-arm64",
        ),
    ]


def test_acceptance_authority_rejects_wrong_publication_object_root(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    reports = _complete_gate_reports(tmp_path / "reports", publication)
    wrong_root = tmp_path / "wrong-objects"
    wrong_root.mkdir()

    result = subprocess.run(
        _accept_command(
            publication,
            tmp_path / "acceptance",
            reports,
            object_root=wrong_root,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "publication release paths" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_acceptance_authority_rejects_wrong_candidate_release_path(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    reports = _complete_gate_reports(tmp_path / "reports", publication)
    plan = json.loads((publication / "publication-plan.json").read_text())
    release_root = (
        publication
        / f"objects/artifacts/{plan['channel']}/releases/{plan['generation']}"
    )

    result = subprocess.run(
        _accept_command(
            publication,
            tmp_path / "acceptance",
            reports,
            candidate_release=release_root / "acceptance-baseline/release.json",
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "publication release paths" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_acceptance_authority_rejects_cross_generation_release_path(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    reports = _complete_gate_reports(tmp_path / "reports", publication)
    plan = json.loads((publication / "publication-plan.json").read_text())
    cross_generation = (
        publication
        / f"objects/artifacts/{plan['channel']}/releases/{'f' * 64}/release.json"
    )

    result = subprocess.run(
        _accept_command(
            publication,
            tmp_path / "acceptance",
            reports,
            candidate_release=cross_generation,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "candidate generation" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_acceptance_authority_rejects_missing_native_package_object(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    reports = _complete_gate_reports(tmp_path / "reports", publication)
    plan = json.loads((publication / "publication-plan.json").read_text())
    package = (
        publication
        / "objects"
        / f"artifacts/{plan['channel']}/releases/{plan['generation']}/"
        "spark/current/linux-arm64/vonk-forge-agent.deb"
    )
    package.unlink()

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", reports),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "candidate linux-arm64 package object is unavailable" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_acceptance_authority_rejects_fabricated_incomplete_or_changed_spark_proof(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report_root = tmp_path / "reports"
    report_root.mkdir()
    nas = _gate_report(
        report_root / "nas.json",
        publication,
        ACCEPTANCE_GATES
        - AMD64_SPARK_GATES
        - ARM64_SPARK_GATES,
    )
    amd64 = _spark_gate_report(
        report_root / "amd64.json",
        publication,
        AMD64_SPARK_GATES,
        "linux-amd64",
    )
    arm64 = _spark_gate_report(
        report_root / "arm64.json",
        publication,
        ARM64_SPARK_GATES,
        "linux-arm64",
    )
    complete = json.loads(arm64.read_text())

    fabricated = copy.deepcopy(complete)
    fabricated.pop("lifecycle")
    incomplete = copy.deepcopy(complete)
    incomplete["lifecycle"]["completed_phases"].remove("identity-renewed")
    missing_proof = copy.deepcopy(complete)
    missing_proof["lifecycle"]["proof"].pop("canary")
    changed = copy.deepcopy(complete)
    changed["lifecycle"]["proof"]["installation"]["identity"][
        "package_sha256"
    ] = "f" * 64
    reused_pairing = copy.deepcopy(complete)
    reused_pairing["lifecycle"]["proof"]["pairing_grant_use_count"] = 2
    unchanged_serial = copy.deepcopy(complete)
    unchanged_serial["lifecycle"]["proof"]["renewal"][
        "certificate_serial_after"
    ] = "0123456789abcdef"
    accepted_old_serial = copy.deepcopy(complete)
    accepted_old_serial["lifecycle"]["proof"]["renewal"][
        "old_certificate_rejection"
    ]["rejected"] = False
    changed_node = copy.deepcopy(complete)
    changed_node["lifecycle"]["proof"]["node_id_after_renewal"] = (
        "spk_fedcba9876543210fedcba9876543210"
    )
    unchanged_build = copy.deepcopy(complete)
    unchanged_build["lifecycle"]["proof"]["installation"]["identity"][
        "build_sha256"
    ] = "invalid"
    indirect_agent = copy.deepcopy(complete)
    indirect_agent["lifecycle"]["proof"]["direct_agent_health"][
        "transport"
    ] = "controller-proxy"
    changed_graph = copy.deepcopy(complete)
    changed_graph["lifecycle"]["proof"]["publication_graph"]["packages"].pop(
        "linux-amd64"
    )
    false_cdi = copy.deepcopy(complete)
    false_cdi["lifecycle"]["proof"]["synthetic_device"]["provenance"] = (
        "physical-gpu"
    )

    for name, document in {
        "accepted-old-serial": accepted_old_serial,
        "changed-graph": changed_graph,
        "changed-node": changed_node,
        "fabricated": fabricated,
        "false-cdi": false_cdi,
        "indirect-agent": indirect_agent,
        "incomplete": incomplete,
        "missing-proof": missing_proof,
        "reused-pairing": reused_pairing,
        "changed": changed,
        "unchanged-build": unchanged_build,
        "unchanged-serial": unchanged_serial,
    }.items():
        bad_report = report_root / f"{name}.json"
        _canonical(bad_report, document)
        output = tmp_path / f"acceptance-{name}"
        result = subprocess.run(
            _accept_command(publication, output, [nas, amd64, bad_report]),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 2, name
        assert "Spark gate report" in result.stderr, (name, result.stderr)
        assert not (output / "acceptance.json").exists()


def test_acceptance_authority_rejects_previous_spark_job_architecture_ownership(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report_root = tmp_path / "reports"
    report_root.mkdir()
    nas = _gate_report(
        report_root / "nas.json",
        publication,
        ACCEPTANCE_GATES
        - AMD64_SPARK_GATES
        - ARM64_SPARK_GATES,
    )
    amd64 = _spark_gate_report(
        report_root / "amd64.json",
        publication,
        AMD64_SPARK_GATES | {"spark_job"},
        "linux-amd64",
    )
    arm64 = _spark_gate_report(
        report_root / "arm64.json",
        publication,
        ARM64_SPARK_GATES - {"spark_job"},
        "linux-arm64",
    )

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", [nas, amd64, arm64]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Spark gate report" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_workflow_nas_gate_report_is_accepted_and_gate_drift_is_rejected(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    workflow = yaml.load(
        (ROOT / ".github/workflows/installer-publication.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    step = next(
        step
        for step in workflow["jobs"]["nas-acceptance"]["steps"]
        if step["name"] == "Run literal clean NAS and Tailscale configuration acceptance"
    )
    nas_gates = set(json.loads(step["env"]["VONK_ACCEPTANCE_GATE_NAMES"]))
    reports = [
        _gate_report(tmp_path / "nas.json", publication, nas_gates),
        _spark_gate_report(
            tmp_path / "amd64.json",
            publication,
            AMD64_SPARK_GATES,
            "linux-amd64",
        ),
        _spark_gate_report(
            tmp_path / "arm64.json",
            publication,
            ARM64_SPARK_GATES,
            "linux-arm64",
        ),
    ]
    accepted = subprocess.run(_accept_command(publication, tmp_path / "accepted", reports), cwd=ROOT, text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    drifted = _gate_report(tmp_path / "drifted.json", publication, nas_gates - {"nas_workstation"})
    rejected = subprocess.run(_accept_command(publication, tmp_path / "rejected", [drifted, *reports[1:]]), cwd=ROOT, text=True, capture_output=True, check=False)
    assert rejected.returncode == 2
    assert "incomplete" in rejected.stderr


def _promote(
    publication: Path,
    destination: Path,
    receipt: Path,
    signature: Path,
    public_key: Path,
) -> subprocess.CompletedProcess[str]:
    encoded = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "promote",
            "--bundle",
            str(publication),
            "--accepted-evidence",
            str(receipt),
            "--accepted-evidence-signature",
            str(signature),
            "--acceptance-public-key",
            str(public_key),
            "--acceptance-public-key-sha256",
            hashlib.sha256(encoded).hexdigest(),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _publish_accepted(
    publication: Path, destination: Path, acceptance_root: Path
) -> subprocess.CompletedProcess[str]:
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(
        acceptance_root, publication
    )
    return _promote(publication, destination, receipt, signature, public_key)


def test_assembly_has_no_prepublication_acceptance_input(tmp_path: Path) -> None:
    command = _assemble_command(tmp_path, _inputs(tmp_path))

    assert "--accepted-evidence" not in command


def test_candidate_publication_writes_only_immutable_generation_objects(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    destination = tmp_path / "public"

    result = _publish_candidate(publication, destination)

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert operations
    assert {operation["phase"] for operation in operations} == {"immutable"}
    assert not (destination / "nas").exists()
    assert not (destination / "spark").exists()
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_rclone_publication_treats_empty_cat_as_missing_object(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    environment, object_root = _fake_rclone_environment(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable = [entry for entry in plan["objects"] if entry["phase"] == "immutable"]
    assert immutable
    for entry in immutable:
        published = object_root / entry["key"]
        assert published.is_file()
        assert hashlib.sha256(published.read_bytes()).hexdigest() == entry["sha256"]

    (object_root / "nas").write_bytes(b"legacy NAS endpoint\n")
    (object_root / "spark").write_bytes(b"legacy Spark endpoint\n")
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)
    encoded_public_key = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    promoted = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "promote",
            "--bundle",
            str(publication),
            "--accepted-evidence",
            str(receipt),
            "--accepted-evidence-signature",
            str(signature),
            "--acceptance-public-key",
            str(public_key),
            "--acceptance-public-key-sha256",
            hashlib.sha256(encoded_public_key).hexdigest(),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert promoted.returncode == 0, promoted.stderr
    assert (object_root / "nas").read_bytes() == (
        publication / "objects/nas"
    ).read_bytes()
    assert (object_root / "spark").read_bytes() == (
        publication / "objects/spark"
    ).read_bytes()
    assert (object_root / "artifacts/stable/current.manifest").is_file()


def test_promotion_refuses_receipt_for_another_generation_without_moving_channel(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path, publication, generation="f" * 64
    )

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 2
    assert "does not match the candidate generation" in result.stderr
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_promotion_publishes_signed_acceptance_receipt_before_channel_pointer(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    destination = tmp_path / "public"
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert [operation["phase"] for operation in operations[-3:]] == [
        "endpoint",
        "endpoint",
        "pointer",
    ]
    assert operations[0]["phase"] == "acceptance"
    plan = json.loads((publication / "publication-plan.json").read_text())
    acceptance = (
        destination
        / f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    assert acceptance.read_bytes() == receipt.read_bytes()
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()
    assert (destination / "artifacts/stable/current.manifest").is_file()


def test_assemble_builds_complete_immutable_generation_and_final_pointer(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
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
        setup_key = next(
            key
            for key in keys
            if key.endswith(f"/spark/current/{platform}/vonk-spark-setup")
            and "/acceptance-baseline/" not in key
        )
        setup_signature_key = f"{setup_key}.sig"
        assert setup_signature_key in keys
        assert any(
            key.endswith(f"/spark/current/{platform}/vonk-forge-agent.deb")
            for key in keys
        )
        release = json.loads(
            next(
                (publication / "objects").glob(
                    f"artifacts/stable/releases/{plan['generation']}/release.json"
                )
            ).read_text()
        )
        signature = publication / "objects" / setup_signature_key
        signature_record = release["artifacts"][
            f"spark-setup-signature-{platform}"
        ]
        assert signature_record == {
            "path": setup_signature_key,
            "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
            "size": signature.stat().st_size,
        }
        raw_signature = tmp_path / f"{platform}.setup.raw.sig"
        raw_signature.write_bytes(
            base64.b64decode(signature.read_bytes().strip(), validate=True)
        )
        verification = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                inputs["signing_public_key"],
                "-signature",
                raw_signature,
                inputs["spark"][platform],
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert verification.returncode == 0, verification.stderr
    assert {
        entry["key"] for entry in plan["objects"] if entry["phase"] == "endpoint"
    } == {
        "nas",
        "spark",
    }
    assert plan["objects"][-1]["key"] == "artifacts/stable/current.manifest"


def test_assemble_binds_lower_dual_architecture_baseline_without_a_pointer(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable = {
        entry["key"]: entry
        for entry in plan["objects"]
        if entry["phase"] == "immutable"
    }
    baseline_releases = [
        key
        for key in immutable
        if key.endswith("/acceptance-baseline/release.json")
    ]

    assert len(baseline_releases) == 1
    baseline_release_path = publication / "objects" / baseline_releases[0]
    baseline_release = json.loads(baseline_release_path.read_text())
    assert baseline_release["version"] == inputs["baseline_version"]
    assert baseline_release["acceptance_only"] is True
    assert set(baseline_release["artifacts"]) >= {
        "agent-package-linux-amd64",
        "agent-package-linux-arm64",
    }
    for record in baseline_release["artifacts"].values():
        path = publication / "objects" / record["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.stat().st_size == record["size"]
    assert all(
        entry["phase"] == "immutable"
        for entry in plan["objects"]
        if "/acceptance-baseline/" in entry["key"]
    )
    ordered = subprocess.run(
        [
            "/usr/bin/dpkg",
            "--compare-versions",
            str(inputs["version"]),
            "gt",
            str(inputs["baseline_version"]),
        ],
        check=False,
    )
    assert ordered.returncode == 0


def test_bootstraps_pin_final_digests_and_immutable_generation_urls(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())
    generation = plan["generation"]

    for kind in ("nas", "spark"):
        bootstrap = next(
            (publication / "objects").glob(
                f"artifacts/stable/releases/{generation}/bootstraps/{kind}"
            )
        ).read_text()
        assert (
            f"https://install.vonkforge.ai/artifacts/stable/releases/{generation}"
            in bootstrap
        )
        assert "@[" not in bootstrap
    nas = (
        publication
        / "objects"
        / f"artifacts/stable/releases/{generation}/bootstraps/nas"
    ).read_text()
    spark = (
        publication
        / "objects"
        / f"artifacts/stable/releases/{generation}/bootstraps/spark"
    ).read_text()
    for path in inputs["nas"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in nas
    for path in inputs["spark"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in spark
    for path in inputs["packages"].values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in spark
    assert hashlib.sha256(inputs["payload"].read_bytes()).hexdigest() in nas


def test_development_uses_the_same_signed_channel_flow_under_dev_paths(
    tmp_path: Path,
) -> None:
    inputs = _inputs(
        tmp_path,
        channel="dev",
        version="0.1.0~dev.4+g" + SOURCE_SHA[:12],
    )
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())
    endpoint_keys = {
        entry["key"] for entry in plan["objects"] if entry["phase"] == "endpoint"
    }

    assert endpoint_keys == {"dev/nas", "dev/spark"}
    assert plan["objects"][-1]["key"] == "artifacts/dev/current.manifest"
    assert b"channel='dev'" in (publication / "objects/dev/nas").read_bytes()


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


def test_promotion_refuses_rejected_acceptance_evidence(tmp_path: Path) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "acceptance", publication, status="rejected"
    )

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 2
    assert "evidence is not accepted" in result.stderr
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_assemble_refuses_an_expired_channel_manifest(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    command[command.index("--expires-at") + 1] = "1"

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "expiry is not in the future" in result.stderr


def test_assemble_rejects_package_metadata_from_another_release(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    wrong = _agent_package(tmp_path / "wrong", "linux-amd64", "1.2.2")
    renamed = tmp_path / "vonk-forge-agent_1.2.3_amd64.deb"
    renamed.write_bytes(wrong.read_bytes())
    inputs["packages"]["linux-amd64"] = renamed

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "package metadata is inconsistent" in result.stderr


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


def test_promotion_writes_signed_atomic_manifest_after_acceptance_and_static_endpoints(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    destination = tmp_path / "public"

    result = _publish_accepted(
        publication, destination, tmp_path / "acceptance"
    )

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert operations[-1] == {
        "key": "artifacts/stable/current.manifest",
        "phase": "pointer",
    }
    assert all(item["phase"] == "acceptance" for item in operations[:-3])
    assert [item["phase"] for item in operations[-3:]] == [
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = destination / "artifacts/stable/current.manifest"
    lines = pointer.read_text().splitlines()
    assert lines[0] == "schema_version=1"
    assert lines[1] == "channel=stable"
    assert lines[-1].startswith("signature=")
    claims = tmp_path / "claims"
    signature = tmp_path / "signature"
    claims.write_text("\n".join(lines[:-1]) + "\n")
    signature.write_bytes(base64.b64decode(lines[-1].removeprefix("signature=")))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            publication / "channel-public-key.pem",
            "-signature",
            signature,
            claims,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()


def test_static_endpoints_do_not_change_between_release_generations(
    tmp_path: Path,
) -> None:
    first_inputs = _inputs(tmp_path / "first")
    first = _assemble(tmp_path / "first", first_inputs)
    second_root = tmp_path / "second"
    second_inputs = _inputs(second_root)
    second_inputs["signing_key"] = first_inputs["signing_key"]
    second_inputs["signing_public_key"] = first_inputs["signing_public_key"]
    _canonical(
        second_inputs["payload"],
        {"compose": "new pinned generation", "schema_version": 1},
    )
    command = _assemble_command(second_root, second_inputs)
    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    second = second_root / "publication"
    assert (first / "objects/nas").read_bytes() == (second / "objects/nas").read_bytes()
    assert (first / "objects/spark").read_bytes() == (
        second / "objects/spark"
    ).read_bytes()


def test_stable_publication_refuses_version_rollback_before_writing(
    tmp_path: Path,
) -> None:
    newest_inputs = _inputs(tmp_path / "newest", version="1.2.4")
    newest = _assemble(tmp_path / "newest", newest_inputs)
    destination = tmp_path / "public"
    first = _publish_accepted(
        newest, destination, tmp_path / "newest-acceptance"
    )
    assert first.returncode == 0, first.stderr

    older_inputs = _inputs(tmp_path / "older", version="1.2.3")
    older_inputs["signing_key"] = newest_inputs["signing_key"]
    older_inputs["signing_public_key"] = newest_inputs["signing_public_key"]
    older = _assemble(tmp_path / "older", older_inputs)
    candidate = _publish_candidate(older, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "older-acceptance", older
    )
    before = (destination / "artifacts/stable/current.manifest").read_bytes()
    rollback = _promote(older, destination, receipt, signature, public_key)

    assert rollback.returncode == 2
    assert "refusing to regress" in rollback.stderr
    assert (destination / "artifacts/stable/current.manifest").read_bytes() == before


def test_refresh_extends_signed_manifest_after_verifying_all_release_objects(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(
        publication, destination, tmp_path / "acceptance"
    )
    assert published.returncode == 0, published.stderr
    manifest = destination / "artifacts/stable/current.manifest"
    before = manifest.read_text().splitlines()
    expires_at = int(time.time()) + 14 * 24 * 60 * 60

    refreshed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "refresh",
            "--channel",
            "stable",
            "--expires-at",
            str(expires_at),
            "--signing-key",
            str(inputs["signing_key"]),
            "--signing-public-key",
            str(inputs["signing_public_key"]),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert refreshed.returncode == 0, refreshed.stderr
    after = manifest.read_text().splitlines()
    assert after[:5] == before[:5]
    assert after[5] == f"expires_at={expires_at}"
    assert after[6:14] == before[6:14]
    assert after[14] != before[14]
    assert json.loads(refreshed.stdout) == {
        "channel": "stable",
        "phase": "pointer",
        "refreshed": True,
    }


def test_refresh_refuses_to_extend_manifest_with_missing_release_object(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(
        publication, destination, tmp_path / "acceptance"
    )
    assert published.returncode == 0, published.stderr
    manifest = destination / "artifacts/stable/current.manifest"
    before = manifest.read_bytes()
    release_path = next(
        line.removeprefix("release_path=")
        for line in manifest.read_text().splitlines()
        if line.startswith("release_path=")
    )
    (destination / release_path).unlink()

    refreshed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "refresh",
            "--channel",
            "stable",
            "--expires-at",
            str(int(time.time()) + 14 * 24 * 60 * 60),
            "--signing-key",
            str(inputs["signing_key"]),
            "--signing-public-key",
            str(inputs["signing_public_key"]),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert refreshed.returncode == 2
    assert "release object is unavailable" in refreshed.stderr
    assert manifest.read_bytes() == before


def test_public_nas_endpoint_verifies_signed_manifest_before_running_release(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    receipt = tmp_path / "receipt"
    for setup in inputs["nas"].values():
        setup.write_text(
            '#!/bin/sh\nset -eu\nprintf \'%s\\n\' "$*" > "$VONK_TEST_RECEIPT"\n'
        )
        setup.chmod(0o755)
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(
        publication, destination, tmp_path / "acceptance"
    )
    assert published.returncode == 0, published.stderr
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\ndestination=\nurl=\n"
        "while [ $# -gt 0 ]; do case $1 in -o) destination=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n"
        'path=${url#https://install.example.test/}\ncp "$VONK_TEST_PUBLIC/$path" "$destination"\n'
    )
    curl.chmod(0o755)
    uname = commands / "uname"
    uname.write_text(
        "#!/bin/sh\ncase ${1:-} in -s) echo Linux;; -m) echo x86_64;; esac\n"
    )
    uname.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "VONK_INSTALL_BASE_URL": "https://install.example.test",
        "VONK_TEST_PUBLIC": str(destination),
        "VONK_TEST_RECEIPT": str(receipt),
    }

    result = subprocess.run(
        ["sh", destination / "nas"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert receipt.read_text().startswith("--template ")
    receipt.unlink()
    manifest = destination / "artifacts/stable/current.manifest"
    manifest.write_bytes(
        manifest.read_bytes().replace(b"version=1.2.3", b"version=1.2.4")
    )
    rejected = subprocess.run(
        ["sh", destination / "nas"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "signature is invalid" in rejected.stderr
    assert not receipt.exists()
