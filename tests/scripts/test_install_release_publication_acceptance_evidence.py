"""Install release publication: acceptance evidence."""

from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.scripts.install_release_publication_support import (
    ACCEPTANCE_GATES,
    ARM64_SPARK_GATES,
    DIGEST,
    ROOT,
    SCRIPT,
    SOURCE_SHA,
    _accept_command,
    _acceptance_receipt,
    _assemble,
    _canonical,
    _gate_report,
    _inputs,
    _promote,
    _promote_command,
    _publish_candidate,
    _spark_gate_report,
)

pytestmark = pytest.mark.needs_dpkg_deb


NAS_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
    "nas_site_secret_preservation",
    "nas_workstation",
}


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
    [
        ("channel", "dev"),
        ("gates", sorted(NAS_GATES - {"nas_workstation"})),
        ("generation", "c" * 64),
        ("run_id", 123457),
        ("source_sha", "d" * 40),
        ("status", "failed"),
        ("tailscale_mode", "full"),
        ("version", "1.2.4"),
    ],
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
