from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import install_release_publication_support as _support
import pytest
import yaml
from install_release_publication_support import (
    ACCEPTANCE_GATES,
    DIGEST,
    ROOT,
    SCRIPT,
    SOURCE_SHA,
    SPARK_PLATFORMS,
    _acceptance_receipt,
    _assemble,
    _assemble_command,
    _canonical,
    _input_mapping,
    _inputs,
    _promote,
    _promote_command,
    _publish_candidate,
)

assembled_publication = _support.assembled_publication
publication = _support.publication

pytestmark = pytest.mark.needs_dpkg_deb


NAS_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
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
        kind == "endpoint" and key in {"nas", "spark", "vonkctl"}
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

def fault(name: str, key: str) -> bool:
    limit = int(os.environ.get(name, "0"))
    if limit <= 0:
        return False
    counters = object_root.parent / "faults"
    counters.mkdir(exist_ok=True)
    counter = counters / (name + "-" + key.replace("/", "_"))
    with counter.open("a+", encoding="utf-8") as state:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        state.seek(0)
        raw = state.read()
        seen = int(raw) if raw else 0
        state.seek(0)
        state.truncate()
        state.write(str(seen + 1))
    return seen < limit

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
    if path.is_file() and fault("FAKE_RCLONE_CAT_FAILS", path.name):
        raise SystemExit(1)
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
        if fault("FAKE_RCLONE_PARTIAL_COPIES", destination.name):
            destination.write_bytes(source.read_bytes()[:-1] + b"?")
            raise SystemExit(0)
        shutil.copyfile(source, destination)
        if fault("FAKE_RCLONE_NOT_IMPLEMENTED_COPIES", destination.name):
            print("NotImplemented: Not Implemented", file=sys.stderr)
            raise SystemExit(1)
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
    assert not (tmp_path / "combined.json").exists()
    fresh = subprocess.run(
        _combine_nas_command(tmp_path, [native, docker]),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert fresh.returncode == 0, fresh.stderr
    assert (tmp_path / "combined.json").is_file()


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
    graph_packages = graph["packages"]
    assert isinstance(graph_packages, dict)
    package_record = graph_packages[platform]
    assert isinstance(package_record, dict)
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
                "package_sha256": package_record["candidate_sha256"],
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
        report["lifecycle"]["proof"]["installation"]["identity"]["package_sha256"] = (
            invented_digest
        )
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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


def _proof(document: dict) -> dict:
    return document["lifecycle"]["proof"]


def _set(path: tuple[str, ...], value: object):
    def tamper(document: dict) -> None:
        target = _proof(document)
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return tamper


# Each forged or incomplete ARM64 Spark lifecycle proof must be refused.
_SPARK_PROOF_TAMPERING = {
    "fabricated": lambda document: document.pop("lifecycle"),
    "incomplete": lambda document: document["lifecycle"]["completed_phases"].remove(
        "identity-renewed"
    ),
    "missing-proof": lambda document: _proof(document).pop("canary"),
    "changed": _set(("installation", "identity", "package_sha256"), "f" * 64),
    "reused-pairing": _set(("pairing_grant_use_count",), 2),
    "unchanged-serial": _set(
        ("renewal", "certificate_serial_after"), "0123456789abcdef"
    ),
    "accepted-old-serial": _set(
        ("renewal", "old_certificate_rejection", "rejected"), False
    ),
    "changed-node": _set(
        ("node_id_after_renewal",), "spk_fedcba9876543210fedcba9876543210"
    ),
    "unchanged-build": _set(("installation", "identity", "build_sha256"), "invalid"),
    "indirect-agent": _set(("direct_agent_health", "transport"), "controller-proxy"),
    "changed-graph": lambda document: _proof(document)["publication_graph"][
        "packages"
    ].pop("linux-arm64"),
    "false-cdi": _set(("synthetic_device", "provenance"), "physical-gpu"),
}


@pytest.fixture(scope="module")
def accepted_spark_proof(
    tmp_path_factory: pytest.TempPathFactory,
    assembled_publication: Path,
) -> tuple[Path, Path, dict]:
    """One assembled publication with a valid NAS report and ARM64 proof."""

    tmp_path = tmp_path_factory.mktemp("spark-proof")
    publication = assembled_publication
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
    return publication, nas, json.loads(arm64.read_text())


@pytest.mark.parametrize("name", sorted(_SPARK_PROOF_TAMPERING))
def test_acceptance_authority_rejects_fabricated_incomplete_or_changed_spark_proof(
    accepted_spark_proof: tuple[Path, Path, dict],
    tmp_path: Path,
    name: str,
) -> None:
    publication, nas, complete = accepted_spark_proof
    document = copy.deepcopy(complete)
    _SPARK_PROOF_TAMPERING[name](document)
    bad_report = tmp_path / f"{name}.json"
    _canonical(bad_report, document)
    output = tmp_path / "acceptance"
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
    publication: Path,
) -> None:
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


def test_installer_acceptance_signer_requires_current_gate_report_set() -> None:
    publication = yaml.load(
        (ROOT / ".github/workflows/release-acceptance-core.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    acceptance = publication["jobs"]["acceptance"]
    assert acceptance["needs"] == [
        "nas-acceptance",
        "spark-acceptance",
        "spark-upgrade-acceptance",
        "spark-recipe-refresh",
        "match-pr-acceptance",
    ]
    # The upgrade-carry and recipe-refresh lanes gate signing: a candidate
    # that drops a serving workload across the upgrade, or cannot refresh a
    # recipe, is never signed. Each keeps its own report artifact; the signed
    # gate set stays the NAS and Spark lane reports.
    assert "needs['spark-upgrade-acceptance'].result == 'success'" in acceptance["if"]
    assert "needs['spark-recipe-refresh'].result == 'success'" in acceptance["if"]
    # The Spark lane may be absent only for a matched pull request payload;
    # its Spark report is then bound through accept --reuse-gate-report.
    condition = " ".join(acceptance["if"].split())
    assert (
        "(needs['spark-acceptance'].result == 'success' || "
        "(needs['match-pr-acceptance'].outputs.reuse == 'true' && "
        "needs['spark-acceptance'].result == 'skipped'))"
    ) in condition
    signing = next(
        step
        for step in acceptance["steps"]
        if step["name"] == "Bind and sign complete acceptance"
    )
    assert 'test "${#reports[@]}" = 2' in signing["run"]


def test_workflow_nas_gate_report_is_accepted_and_gate_drift_is_rejected(
    tmp_path: Path,
    publication: Path,
) -> None:
    plan = json.loads((publication / "publication-plan.json").read_text())
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-acceptance-core.yml").read_text(),
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


def test_preflight_validates_without_writing_publication_objects(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "publication", inputs)
    destination = tmp_path / "destination"
    published = _publish_candidate(publication, destination)
    assert published.returncode == 0, published.stderr
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "publication", publication
    )
    before = sorted(
        str(path.relative_to(destination)) for path in destination.rglob("*")
    )
    result = subprocess.run(
        _promote_command(
            publication,
            receipt,
            signature,
            public_key,
            filesystem=destination,
            preflight_only=True,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "channel": "stable",
        "generation": json.loads((publication / "publication-plan.json").read_text())[
            "generation"
        ],
        "phase": "preflight",
    }
    assert before == sorted(
        str(path.relative_to(destination)) for path in destination.rglob("*")
    )


def test_candidate_publication_writes_only_immutable_generation_objects(
    tmp_path: Path,
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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


@pytest.mark.parametrize(
    "fault",
    [
        {"FAKE_RCLONE_NOT_IMPLEMENTED_COPIES": "1"},
        {"FAKE_RCLONE_CAT_FAILS": "2"},
        {"FAKE_RCLONE_PARTIAL_COPIES": "1"},
    ],
)
def test_rclone_candidate_publication_survives_transient_r2_faults(
    tmp_path: Path,
    fault: dict[str, str],
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    environment["INSTALL_RELEASE_PUBLICATION_RETRY_DELAY"] = "0"
    environment.update(fault)

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
    for entry in plan["objects"]:
        if entry["phase"] == "immutable":
            published = object_root / entry["key"]
            assert hashlib.sha256(published.read_bytes()).hexdigest() == entry["sha256"]


def test_rclone_candidate_verification_stays_strict_when_object_never_matches(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, _ = _fake_rclone_environment(tmp_path)
    environment["INSTALL_RELEASE_PUBLICATION_RETRY_DELAY"] = "0"
    environment["FAKE_RCLONE_PARTIAL_COPIES"] = "100"

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
    assert "R2 verification failed" in result.stderr


def test_rclone_candidate_objects_publish_with_bounded_parallelism(
    tmp_path: Path,
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    assert [operation["phase"] for operation in operations[-4:]] == [
        "endpoint",
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert [operation["phase"] for operation in operations[-4:]] == [
        "endpoint",
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
    publication: Path,
) -> None:
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
    publication: Path,
) -> None:
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
    phase_order = {"immutable": 0, "endpoint": 1, "pointer": 2}
    assert phases == sorted(phases, key=lambda phase: phase_order[phase])
    assert phases[-1] == "pointer"
    assert phases.count("pointer") == 1
    keys = {entry["key"] for entry in plan["objects"]}
    assert any(
        key.endswith("/nas/current/" + Path(str(inputs["nas_wheel"])).name)
        for key in keys
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
                str(inputs["signing_public_key"]),
                "-signature",
                raw_signature,
                str(_input_mapping(inputs, "spark")[platform]),
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
        "vonkctl",
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
    candidate = json.loads(
        baseline_release_path.parent.parent.joinpath("release.json").read_text()
    )
    # The lower agent carries the same Controller, so its observer must consume
    # that Controller's signed, wheel-bundled contract too.
    assert (
        baseline_release["artifacts"]["cli-wheel"]
        == candidate["artifacts"]["cli-wheel"]
    )
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


@pytest.mark.parametrize(
    "missing",
    ("spark-linux-arm64", "package-linux-arm64"),
)
def test_assemble_rejects_incomplete_platform_matrix(
    tmp_path: Path, missing: str
) -> None:
    inputs = _inputs(tmp_path)
    kind, platform = missing.split("-", 1)
    collection = _input_mapping(
        inputs,
        {"spark": "spark", "package": "packages"}[kind],
    )
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
