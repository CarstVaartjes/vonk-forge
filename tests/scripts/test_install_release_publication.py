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
SPARK_PLATFORMS = ("linux-arm64",)
ACCEPTANCE_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
    "compose_hermes",
    "nas_site_secret_preservation",
    "nas_workstation",
    "spark_arm64",
    "spark_job",
    "spark_pairing",
    "spark_renewal",
}
NAS_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
    "compose_hermes",
    "nas_site_secret_preservation",
    "nas_workstation",
}
ARM64_SPARK_GATES = {"spark_arm64", "spark_job", "spark_pairing", "spark_renewal"}
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
            "subject": [{"digest": {"sha256": "d" * 64}, "name": "vonk-agent"}],
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
    _canonical(
        payload,
        {
            "docker_compose_yaml": "services: {}\n",
            "schema_version": 2,
        },
    )
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
        "--images-source-sha",
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
        "--litellm-image",
        f"ghcr.io/carstvaartjes/vonk-forge-litellm:{image_tag}@sha256:{DIGEST}",
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


def _publish_candidate(
    publication: Path, destination: Path
) -> subprocess.CompletedProcess[str]:
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
import fcntl
import os
import shutil
import sys
import time
from pathlib import Path

command = sys.argv[1]
arguments = sys.argv[2:]
object_root = Path(os.environ["FAKE_RCLONE_ROOT"])

def object_path(value: str) -> Path:
    remote_path = value.split(":", 1)[1]
    key = remote_path.split("/", 1)[1]
    return object_root / key

def update_activity(delta: int) -> None:
    activity_path = os.environ.get("FAKE_RCLONE_ACTIVITY")
    if activity_path is None:
        return
    with Path(activity_path).open("a+", encoding="utf-8") as activity:
        fcntl.flock(activity.fileno(), fcntl.LOCK_EX)
        activity.seek(0)
        raw = activity.read()
        state = json.loads(raw) if raw else {"active": 0, "maximum": 0}
        state["active"] += delta
        state["maximum"] = max(state["maximum"], state["active"])
        activity.seek(0)
        activity.truncate()
        json.dump(state, activity)
        activity.flush()
        fcntl.flock(activity.fileno(), fcntl.LOCK_UN)

def wait_for_peer(kind: str, key: str) -> None:
    barrier = os.environ.get("FAKE_RCLONE_PARALLEL_BARRIER")
    if barrier is None:
        return
    selected = (
        kind == "read" and "/releases/" in key and "/acceptance/" not in key
    ) or (
        kind == "acceptance" and "/acceptance/" in key
    ) or (
        kind == "endpoint" and key in {"nas", "spark"}
    )
    if not selected:
        return
    markers = Path(barrier) / kind
    markers.mkdir(parents=True, exist_ok=True)
    (markers / str(os.getpid())).touch()
    deadline = time.monotonic() + 2
    while len(list(markers.iterdir())) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)

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
    wait_for_peer("read", str(path.relative_to(object_root)))
    # rclone cat succeeds with no output when its exact source object is absent.
    if path.is_file():
        sys.stdout.buffer.write(path.read_bytes())
elif command == "copyto":
    update_activity(1)
    try:
        time.sleep(float(os.environ.get("FAKE_RCLONE_COPY_DELAY", "0")))
        source = Path(arguments[-2])
        destination = object_path(arguments[-1])
        key = str(destination.relative_to(object_root))
        wait_for_peer("acceptance", key)
        wait_for_peer("endpoint", key)
        if "--immutable" in arguments and destination.exists():
            raise SystemExit(9)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    finally:
        update_activity(-1)
elif command == "deletefile":
    path = object_path(arguments[0])
    if not path.is_file():
        raise SystemExit(3)
    path.unlink()
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
            "lanes": ["docker-29.4.3", "native"],
            "run_id": run_id,
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "status": "passed",
            "tailscale_modes": {
                "docker-29.4.3": "disabled",
                "native": "full",
            },
            "version": plan["version"],
        },
    )
    return path


def _nas_lane_report(
    path: Path,
    lane: str,
    *,
    channel: str = "stable",
    generation: str = DIGEST,
    run_id: int = 123456,
    source_sha: str = SOURCE_SHA,
    status: str = "passed",
    version: str = "1.2.3",
) -> Path:
    tailscale_mode = {
        "docker-29.4.3": "disabled",
        "native": "full",
    }[lane]
    _canonical(
        path,
        {
            "channel": channel,
            "gates": sorted(NAS_GATES),
            "generation": generation,
            "lane": lane,
            "run_id": run_id,
            "schema_version": 2,
            "source_sha": source_sha,
            "status": status,
            "tailscale_mode": tailscale_mode,
            "version": version,
        },
    )
    return path


def _combine_nas_command(
    tmp_path: Path,
    reports: list[Path],
    *,
    channel: str = "stable",
    generation: str = DIGEST,
    run_id: int = 123456,
    source_sha: str = SOURCE_SHA,
    version: str = "1.2.3",
) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "combine-nas-evidence",
        "--channel",
        channel,
        "--version",
        version,
        "--source-sha",
        source_sha,
        "--generation",
        generation,
        "--run-id",
        str(run_id),
        "--output",
        str(tmp_path / "combined.json"),
    ]
    for report in reports:
        command.extend(("--lane-report", str(report)))
    return command


def test_nas_evidence_combiner_requires_both_exact_candidate_lanes(
    tmp_path: Path,
) -> None:
    reports = [
        _nas_lane_report(tmp_path / "native.json", "native"),
        _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3"),
    ]

    result = subprocess.run(
        _combine_nas_command(tmp_path, reports),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("sha256:")
    assert json.loads((tmp_path / "combined.json").read_text()) == {
        "channel": "stable",
        "gates": sorted(NAS_GATES),
        "generation": DIGEST,
        "lanes": ["docker-29.4.3", "native"],
        "run_id": 123456,
        "schema_version": 2,
        "source_sha": SOURCE_SHA,
        "status": "passed",
        "tailscale_modes": {
            "docker-29.4.3": "disabled",
            "native": "full",
        },
        "version": "1.2.3",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("channel", "dev"),
        ("gates", sorted(NAS_GATES - {"nas_workstation"})),
        ("generation", "c" * 64),
        ("run_id", 123457),
        ("source_sha", "d" * 40),
        ("status", "failed"),
        ("tailscale_mode", "full"),
        ("version", "1.2.4"),
    ),
)
def test_nas_evidence_combiner_rejects_mismatched_lane_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    native = _nas_lane_report(tmp_path / "native.json", "native")
    docker = _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3")
    document = json.loads(docker.read_text())
    document[field] = value
    _canonical(docker, document)

    result = subprocess.run(
        _combine_nas_command(tmp_path, [native, docker]),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "does not match the candidate" in result.stderr
    assert not (tmp_path / "combined.json").exists()


def test_nas_evidence_combiner_rejects_duplicate_or_unsafe_lane_reports(
    tmp_path: Path,
) -> None:
    native = _nas_lane_report(tmp_path / "native.json", "native")
    duplicate = _nas_lane_report(tmp_path / "duplicate.json", "native")
    duplicate_result = subprocess.run(
        _combine_nas_command(tmp_path, [native, duplicate]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert duplicate_result.returncode == 2
    assert not (tmp_path / "combined.json").exists()

    docker = _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3")
    hostile_document = json.loads(docker.read_text())
    hostile_document["lane"] = []
    _canonical(docker, hostile_document)
    hostile_result = subprocess.run(
        _combine_nas_command(tmp_path, [native, docker]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert hostile_result.returncode == 2
    assert "does not match the candidate" in hostile_result.stderr
    assert not (tmp_path / "combined.json").exists()

    docker = _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3")
    boolean_schema = json.loads(docker.read_text())
    boolean_schema["schema_version"] = True
    _canonical(docker, boolean_schema)
    boolean_schema_result = subprocess.run(
        _combine_nas_command(tmp_path, [native, docker]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert boolean_schema_result.returncode == 2
    assert "lane report is invalid" in boolean_schema_result.stderr
    assert not (tmp_path / "combined.json").exists()

    docker = _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3")
    boolean_run_id = json.loads(docker.read_text())
    boolean_run_id["run_id"] = True
    _canonical(docker, boolean_run_id)
    boolean_run_id_result = subprocess.run(
        _combine_nas_command(tmp_path, [native, docker]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert boolean_run_id_result.returncode == 2
    assert "does not match the candidate" in boolean_run_id_result.stderr
    assert not (tmp_path / "combined.json").exists()

    docker = _nas_lane_report(tmp_path / "docker.json", "docker-29.4.3")
    unsafe = tmp_path / "unsafe.json"
    unsafe.symlink_to(docker)
    unsafe_result = subprocess.run(
        _combine_nas_command(tmp_path, [native, unsafe]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert unsafe_result.returncode == 2
    assert "unavailable or unsafe" in unsafe_result.stderr
    assert not (tmp_path / "combined.json").exists()


def _actual_publication_graph(publication: Path, platform: str) -> dict[str, object]:
    plan = json.loads((publication / "publication-plan.json").read_text())
    object_root = publication / "objects"
    release_root = (
        object_root / f"artifacts/{plan['channel']}/releases/{plan['generation']}"
    )
    candidate = json.loads((release_root / "release.json").read_text())
    baseline = json.loads(
        (release_root / "acceptance-baseline/release.json").read_text()
    )
    packages: dict[str, dict[str, str]] = {}
    for native_platform in SPARK_PLATFORMS:
        candidate_record = candidate["artifacts"][f"agent-package-{native_platform}"]
        baseline_record = baseline["artifacts"][f"agent-package-{native_platform}"]
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
        "verified_platforms": ["linux-arm64"],
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
    assert platform == "linux-arm64"
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
        ACCEPTANCE_GATES - ARM64_SPARK_GATES,
    )
    arm64 = _spark_gate_report(
        report_root / "arm64.json",
        publication,
        ARM64_SPARK_GATES,
        "linux-arm64",
    )
    invented_digest = "f" * 64
    for report_path in (arm64,):
        report = json.loads(report_path.read_text())
        graph = report["lifecycle"]["proof"]["publication_graph"]
        graph["packages"]["linux-arm64"]["candidate_sha256"] = invented_digest
        graph["candidate_package_sha256"] = invented_digest
        report["lifecycle"]["proof"]["installation"]["identity"][
            "package_sha256"
        ] = invented_digest
        _canonical(report_path, report)

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", [nas, arm64]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "publication graph" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def _complete_gate_reports(report_root: Path, publication: Path) -> list[Path]:
    report_root.mkdir()
    return [
        _gate_report(
            report_root / "nas.json",
            publication,
            ACCEPTANCE_GATES - ARM64_SPARK_GATES,
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
        ACCEPTANCE_GATES - ARM64_SPARK_GATES,
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
    changed["lifecycle"]["proof"]["installation"]["identity"]["package_sha256"] = (
        "f" * 64
    )
    reused_pairing = copy.deepcopy(complete)
    reused_pairing["lifecycle"]["proof"]["pairing_grant_use_count"] = 2
    unchanged_serial = copy.deepcopy(complete)
    unchanged_serial["lifecycle"]["proof"]["renewal"]["certificate_serial_after"] = (
        "0123456789abcdef"
    )
    accepted_old_serial = copy.deepcopy(complete)
    accepted_old_serial["lifecycle"]["proof"]["renewal"]["old_certificate_rejection"][
        "rejected"
    ] = False
    changed_node = copy.deepcopy(complete)
    changed_node["lifecycle"]["proof"]["node_id_after_renewal"] = (
        "spk_fedcba9876543210fedcba9876543210"
    )
    unchanged_build = copy.deepcopy(complete)
    unchanged_build["lifecycle"]["proof"]["installation"]["identity"][
        "build_sha256"
    ] = "invalid"
    indirect_agent = copy.deepcopy(complete)
    indirect_agent["lifecycle"]["proof"]["direct_agent_health"]["transport"] = (
        "controller-proxy"
    )
    changed_graph = copy.deepcopy(complete)
    changed_graph["lifecycle"]["proof"]["publication_graph"]["packages"].pop(
        "linux-arm64"
    )
    false_cdi = copy.deepcopy(complete)
    false_cdi["lifecycle"]["proof"]["synthetic_device"]["provenance"] = "physical-gpu"

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
            _accept_command(publication, output, [nas, bad_report]),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 2, name
        expected_error = (
            "behavioral gate report" if name == "fabricated" else "Spark gate report"
        )
        assert expected_error in result.stderr, (name, result.stderr)
        assert not (output / "acceptance.json").exists()


def test_acceptance_authority_rejects_incomplete_arm64_gate_ownership(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    report_root = tmp_path / "reports"
    report_root.mkdir()
    nas = _gate_report(
        report_root / "nas.json",
        publication,
        ACCEPTANCE_GATES - ARM64_SPARK_GATES,
    )
    arm64 = _spark_gate_report(
        report_root / "arm64.json",
        publication,
        ARM64_SPARK_GATES - {"spark_job"},
        "linux-arm64",
    )

    result = subprocess.run(
        _accept_command(publication, tmp_path / "acceptance", [nas, arm64]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Spark gate report" in result.stderr
    assert not (tmp_path / "acceptance/acceptance.json").exists()


def test_installer_publication_has_one_development_fanin_anchor() -> None:
    source = yaml.load(
        (ROOT / ".github/workflows/installer-publication-source.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    publication = yaml.load(
        (ROOT / ".github/workflows/installer-publication.yml").read_text(),
        Loader=yaml.BaseLoader,
    )

    producer_paths: set[str] = set()
    for name in ("agent-release.yml", "dev-images.yml", "installer-setups.yml"):
        producer = yaml.load(
            (ROOT / ".github/workflows" / name).read_text(),
            Loader=yaml.BaseLoader,
        )
        producer_paths.update(producer["on"]["push"]["paths"])
    producer_paths.update(
        {
            ".github/workflows/installer-publication-source.yml",
            ".github/workflows/installer-publication.yml",
            "install/**",
            "scripts/build-nas-compose-bundle",
            "scripts/install-release-publication",
            "tests/acceptance/test_fresh_nas_install.py",
            "tests/acceptance/test_spark_lifecycle.py",
        }
    )
    assert source["on"]["push"]["branches"] == ["main"]
    assert set(source["on"]["push"]["paths"]) == producer_paths
    assert publication["on"]["workflow_run"]["workflows"] == [
        "CI",
        "Installer publication source",
    ]
    assert "schedule" not in publication["on"]
    authority = publication["jobs"]["authority"]
    assert "Installer publication source" in authority["if"]
    assert "Development images" not in authority["if"]
    bind = next(
        step
        for step in authority["steps"]
        if step["name"] == "Bind accepted workflow evidence to source authority"
    )
    script = bind["run"]
    assert script.count("sleep 60") == 1
    assert script.index("sleep 60") < script.index(
        "development_run agent-release.yml"
    )


def test_installer_acceptance_signer_requires_current_gate_report_set() -> None:
    publication = yaml.load(
        (ROOT / ".github/workflows/installer-publication.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    acceptance = publication["jobs"]["acceptance"]
    assert acceptance["needs"] == [
        "authority",
        "candidate",
        "nas-acceptance",
        "spark-acceptance",
    ]
    signing = next(
        step
        for step in acceptance["steps"]
        if step["name"] == "Bind and sign complete acceptance"
    )
    assert 'test "${#reports[@]}" = 2' in signing["run"]


def test_workflow_nas_gate_report_is_accepted_and_gate_drift_is_rejected(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    plan = json.loads((publication / "publication-plan.json").read_text())
    workflow = yaml.load(
        (ROOT / ".github/workflows/installer-publication.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    step = next(
        step
        for step in workflow["jobs"]["nas-lane-acceptance"]["steps"]
        if step["name"] == "Write exact NAS lane evidence"
    )
    nas_gates = set(json.loads(step["env"]["VONK_ACCEPTANCE_GATE_NAMES"]))
    assert nas_gates == NAS_GATES
    lane_reports = [
        _nas_lane_report(
            tmp_path / "native-lane.json",
            "native",
            channel=plan["channel"],
            generation=plan["generation"],
            source_sha=plan["source_sha"],
            version=plan["version"],
        ),
        _nas_lane_report(
            tmp_path / "docker-lane.json",
            "docker-29.4.3",
            channel=plan["channel"],
            generation=plan["generation"],
            source_sha=plan["source_sha"],
            version=plan["version"],
        ),
    ]
    combined = subprocess.run(
        _combine_nas_command(
            tmp_path,
            lane_reports,
            channel=plan["channel"],
            generation=plan["generation"],
            source_sha=plan["source_sha"],
            version=plan["version"],
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert combined.returncode == 0, combined.stderr
    reports = [
        tmp_path / "combined.json",
        _spark_gate_report(
            tmp_path / "arm64.json",
            publication,
            ARM64_SPARK_GATES,
            "linux-arm64",
        ),
    ]
    accepted = subprocess.run(
        _accept_command(publication, tmp_path / "accepted", reports),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    drifted = _gate_report(
        tmp_path / "drifted.json", publication, nas_gates - {"nas_workstation"}
    )
    rejected = subprocess.run(
        _accept_command(publication, tmp_path / "rejected", [drifted, *reports[1:]]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "incomplete" in rejected.stderr


def _promote(
    publication: Path,
    destination: Path,
    receipt: Path,
    signature: Path,
    public_key: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _promote_command(
            publication, receipt, signature, public_key, filesystem=destination
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _promote_command(
    publication: Path,
    receipt: Path,
    signature: Path,
    public_key: Path,
    *,
    filesystem: Path | None = None,
    remote: str | None = None,
) -> list[str]:
    if (filesystem is None) == (remote is None):
        raise AssertionError("select one promotion destination")
    encoded = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    destination = (
        ["--filesystem", str(filesystem)]
        if filesystem is not None
        else ["--rclone-remote", str(remote)]
    )
    return [
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
        *destination,
    ]


def _publish_accepted(
    publication: Path, destination: Path, acceptance_root: Path
) -> subprocess.CompletedProcess[str]:
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(acceptance_root, publication)
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


def test_dev_pointer_quarantine_is_exact_preserving_and_idempotent(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "public"
    pointer = destination / "artifacts/dev/current.manifest"
    pointer.parent.mkdir(parents=True)
    legacy = b"schema_version=1\nchannel=dev\n"
    pointer.write_bytes(legacy)
    digest = hashlib.sha256(legacy).hexdigest()
    command = [
        sys.executable,
        str(SCRIPT),
        "quarantine-dev-pointer",
        "--expected-sha256",
        digest,
        "--filesystem",
        str(destination),
    ]

    quarantined = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert quarantined.returncode == 0, quarantined.stderr
    receipt = json.loads(quarantined.stdout)
    retired = destination / receipt["retired_key"]
    assert receipt["already_quarantined"] is False
    assert not pointer.exists()
    assert retired.read_bytes() == legacy

    repeated = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["already_quarantined"] is True


def test_rclone_dev_pointer_quarantine_refuses_digest_drift_then_preserves(
    tmp_path: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    pointer = object_root / "artifacts/dev/current.manifest"
    pointer.parent.mkdir(parents=True)
    legacy = b"schema_version=1\nchannel=dev\n"
    pointer.write_bytes(legacy)
    digest = hashlib.sha256(legacy).hexdigest()
    base = [
        sys.executable,
        str(SCRIPT),
        "quarantine-dev-pointer",
        "--rclone-remote",
        "r2:vonk-forge-installers",
        "--expected-sha256",
    ]

    drifted = subprocess.run(
        [*base, "0" * 64],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert drifted.returncode == 2
    assert "pointer digest changed" in drifted.stderr
    assert pointer.read_bytes() == legacy

    quarantined = subprocess.run(
        [*base, digest],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert quarantined.returncode == 0, quarantined.stderr
    receipt = json.loads(quarantined.stdout)
    assert not pointer.exists()
    assert (object_root / receipt["retired_key"]).read_bytes() == legacy


def test_rclone_candidate_objects_publish_with_bounded_parallelism(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    environment, _ = _fake_rclone_environment(tmp_path)
    activity = tmp_path / "rclone-activity.json"
    environment["FAKE_RCLONE_ACTIVITY"] = str(activity)
    environment["FAKE_RCLONE_COPY_DELAY"] = "0.5"

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
    assert json.loads(activity.read_text()) == {"active": 0, "maximum": 8}
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable_keys = [
        entry["key"] for entry in plan["objects"] if entry["phase"] == "immutable"
    ]
    receipt_keys = [json.loads(line)["key"] for line in result.stdout.splitlines()]
    assert receipt_keys == immutable_keys

    replay = subprocess.run(
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
    assert replay.returncode == 0, replay.stderr
    assert [json.loads(line)["key"] for line in replay.stdout.splitlines()] == (
        immutable_keys
    )


def test_parallel_rclone_candidate_conflict_is_fail_closed_without_receipt(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    environment, object_root = _fake_rclone_environment(tmp_path)
    plan = json.loads((publication / "publication-plan.json").read_text())
    conflict = next(entry for entry in plan["objects"] if entry["phase"] == "immutable")
    conflict_path = object_root / conflict["key"]
    conflict_path.parent.mkdir(parents=True)
    conflict_path.write_bytes(b"conflicting immutable object\n")

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

    assert result.returncode == 2
    assert "refusing to overwrite immutable object" in result.stderr
    assert result.stdout == ""
    assert conflict_path.read_bytes() == b"conflicting immutable object\n"


def test_rclone_promotion_parallelizes_reads_and_phase_groups_before_pointer(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    environment, object_root = _fake_rclone_environment(tmp_path)
    candidate = subprocess.run(
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
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)
    encoded_public_key = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    barrier = tmp_path / "parallel-barrier"
    environment["FAKE_RCLONE_PARALLEL_BARRIER"] = str(barrier)

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
    assert all(
        len(list((barrier / phase).iterdir())) >= 2
        for phase in (
            "read",
            "acceptance",
            "endpoint",
        )
    )
    operations = [json.loads(line) for line in promoted.stdout.splitlines()]
    assert [operation["phase"] for operation in operations[-3:]] == [
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = object_root / "artifacts/stable/current.manifest"
    assert pointer.is_file()
    plan = json.loads((publication / "publication-plan.json").read_text())
    acceptance = (
        object_root
        / f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    assert acceptance.read_bytes() == receipt.read_bytes()
    assert (object_root / "nas").is_file()
    assert (object_root / "spark").is_file()


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


def test_promotion_replays_existing_signed_acceptance_without_overwrite(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    first_receipt, first_signature, first_public_key = _acceptance_receipt(
        tmp_path / "first", publication
    )
    first = _promote(
        publication, destination, first_receipt, first_signature, first_public_key
    )
    assert first.returncode == 0, first.stderr
    stored_receipt = destination / (
        "artifacts/stable/releases/"
        + json.loads((publication / "publication-plan.json").read_text())["generation"]
        + "/acceptance/receipt.json"
    )
    original = stored_receipt.read_bytes()

    second_receipt, second_signature, second_public_key = _acceptance_receipt(
        tmp_path / "second", publication
    )
    second = _promote(
        publication, destination, second_receipt, second_signature, second_public_key
    )

    assert second.returncode == 0, second.stderr
    assert stored_receipt.read_bytes() == original
    assert (destination / "artifacts/stable/current.manifest").is_file()


def test_rclone_promotion_replays_existing_signed_acceptance_without_overwrite(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "inputs", _inputs(tmp_path / "inputs"))
    environment, object_root = _fake_rclone_environment(tmp_path)
    candidate = subprocess.run(
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
    assert candidate.returncode == 0, candidate.stderr
    first = _acceptance_receipt(tmp_path / "first", publication)
    first_result = subprocess.run(
        _promote_command(
            publication,
            first[0],
            first[1],
            first[2],
            remote="r2:vonk-forge-installers",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first_result.returncode == 0, first_result.stderr
    plan = json.loads((publication / "publication-plan.json").read_text())
    stored_receipt = object_root / (
        f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    original = stored_receipt.read_bytes()

    second = _acceptance_receipt(tmp_path / "second", publication)
    second_result = subprocess.run(
        _promote_command(
            publication,
            second[0],
            second[1],
            second[2],
            remote="r2:vonk-forge-installers",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second_result.returncode == 0, second_result.stderr
    assert stored_receipt.read_bytes() == original


def test_assemble_builds_complete_immutable_generation_and_final_pointer(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())

    assert plan["schema_version"] == 2
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
        signature_record = release["artifacts"][f"spark-setup-signature-{platform}"]
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


def test_assemble_binds_lower_arm64_baseline_without_a_pointer(
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
        key for key in immutable if key.endswith("/acceptance-baseline/release.json")
    ]

    assert len(baseline_releases) == 1
    baseline_release_path = publication / "objects" / baseline_releases[0]
    baseline_release = json.loads(baseline_release_path.read_text())
    assert baseline_release["version"] == inputs["baseline_version"]
    assert baseline_release["acceptance_only"] is True
    assert set(baseline_release["artifacts"]) >= {
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
    ("nas-linux-amd64", "nas-darwin-arm64", "spark-linux-arm64", "package-linux-arm64"),
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
    wrong = _agent_package(tmp_path / "wrong", "linux-arm64", "1.2.2")
    renamed = tmp_path / "vonk-forge-agent_1.2.3_arm64.deb"
    renamed.write_bytes(wrong.read_bytes())
    inputs["packages"]["linux-arm64"] = renamed

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "package metadata is inconsistent" in result.stderr


@pytest.mark.parametrize("role", ("api", "worker", "hermes", "litellm"))
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


def test_development_assembly_reuses_images_from_an_accepted_ancestor(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, channel="dev")
    command = _assemble_command(tmp_path, inputs)
    images_source_sha = "c" * 40
    command[command.index("--images-source-sha") + 1] = images_source_sha
    for role in ("api", "worker", "hermes", "litellm"):
        option = f"--{role}-image"
        command[command.index(option) + 1] = command[
            command.index(option) + 1
        ].replace(f"dev-sha-{SOURCE_SHA}", f"dev-sha-{images_source_sha}")

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_development_assembly_rejects_images_outside_bound_accepted_run(
    tmp_path: Path,
) -> None:
    command = _assemble_command(tmp_path, _inputs(tmp_path, channel="dev"))
    command[command.index("--images-source-sha") + 1] = "c" * 40

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "api image version is inconsistent" in result.stderr


def test_promotion_writes_signed_atomic_manifest_after_acceptance_and_static_endpoints(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    destination = tmp_path / "public"

    result = _publish_accepted(publication, destination, tmp_path / "acceptance")

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
    assert lines[0] == "schema_version=2"
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
        {
            "docker_compose_yaml": "services:\n  control-api:\n    image: replacement\n",
            "schema_version": 2,
        },
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
    first = _publish_accepted(newest, destination, tmp_path / "newest-acceptance")
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
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
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
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
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
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
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
