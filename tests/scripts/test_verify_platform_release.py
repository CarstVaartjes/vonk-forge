import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-platform-release"
ACCEPT_UPDATE = ROOT / "scripts/accept-platform-update"
UPDATE_GATES = {
    "physical-control-host-update-recovery",
    "physical-node-canary-rollback",
    "signed-platform-update-manifest-evidence",
}


@pytest.mark.skipif(os.geteuid() == 0, reason="requires an unprivileged owner")
def test_physical_evidence_key_rejects_a_caller_owned_key(tmp_path: Path) -> None:
    load_key = runpy.run_path(str(SCRIPT))["_physical_evidence_key"]
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("33" * 32))
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    path = tmp_path / "caller-owned.json"
    path.write_bytes(
        _canonical(
            {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(public).hexdigest(),
                "public_key": public.hex(),
                "schema_version": 1,
            }
        )
    )
    path.chmod(0o600)

    assert load_key(path) == (None, None)


def test_signed_manifest_claim_binds_the_shared_release_digest() -> None:
    validate = runpy.run_path(str(SCRIPT))["_physical_claims_valid"]
    claims = {
        "agent_payload_sha256": {"linux-arm64": "c" * 64},
        "build_digest": "sha256:" + "e" * 64,
        "platform_target_name": "platform/releases/1.0.0/" + "d" * 64 + ".json",
        "platform_target_sha256": "d" * 64,
        "tuf_targets_version": 7,
    }

    assert (
        validate(
            claims,
            evidence_type="signed-platform-update-manifest",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is False
    )
    assert (
        validate(
            claims,
            evidence_type="signed-platform-update-manifest",
            candidate="1.0.0",
            release_digest="sha256:" + "d" * 64,
        )
        is True
    )


def test_spark_physical_claim_binds_non_ssh_transport() -> None:
    validate = runpy.run_path(str(SCRIPT))["_physical_claims_valid"]
    claims = {
        "architecture": "linux-arm64",
        "canary_node": "spk_" + "1" * 32,
        "from_slot": "A",
        "rollback_evidence_sha256": "f" * 64,
        "rollback_slot": "A",
        "supervisor_generation": 2,
        "target_slot": "B",
    }

    assert (
        validate(
            claims,
            evidence_type="node-canary-and-rollback",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is False
    )
    claims.update(
        {
            "ssh_used_for_standard_path": False,
            "transport": "outbound-mtls-agent-channel",
        }
    )
    assert (
        validate(
            claims,
            evidence_type="node-canary-and-rollback",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is False
    )
    claims.update(
        {
            "from_agent_sha256": "a" * 64,
            "from_platform_version": "0.9.0",
            "target_agent_sha256": "c" * 64,
            "target_build_digest": "sha256:" + "e" * 64,
            "target_platform_version": "1.0.0",
        }
    )
    assert (
        validate(
            claims,
            evidence_type="node-canary-and-rollback",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is True
    )


def test_control_recovery_claim_binds_candidate_generation_to_release() -> None:
    validate = runpy.run_path(str(SCRIPT))["_physical_claims_valid"]
    claims = {
        "candidate_generation": "gen-" + "c" * 24,
        "from_generation": "gen-" + "a" * 24,
        "recovered_generation": "gen-" + "c" * 24,
        "recovery_evidence_sha256": "e" * 64,
        "rollback_evidence_sha256": "f" * 64,
        "rolled_back_generation": "gen-" + "a" * 24,
    }

    assert (
        validate(
            claims,
            evidence_type="control-host-update-recovery",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is False
    )
    claims["candidate_generation"] = "gen-" + "b" * 24
    claims["recovered_generation"] = "gen-" + "b" * 24
    assert (
        validate(
            claims,
            evidence_type="control-host-update-recovery",
            candidate="1.0.0",
            release_digest="sha256:" + "b" * 64,
        )
        is True
    )


def test_node_claim_must_match_the_signed_manifest_content() -> None:
    compatible = runpy.run_path(str(SCRIPT))["_node_claim_matches_manifest"]
    manifest = {
        "agent_payload_sha256": {"linux-arm64": "c" * 64},
        "build_digest": "sha256:" + "e" * 64,
    }
    node = {
        "architecture": "linux-arm64",
        "target_agent_sha256": "d" * 64,
        "target_build_digest": "sha256:" + "e" * 64,
    }

    assert compatible(manifest, node) is False
    node["target_agent_sha256"] = "c" * 64
    assert compatible(manifest, node) is True


def test_release_verifier_lists_external_gates() -> None:
    result = subprocess.run(
        [SCRIPT, "--candidate", "1.0.0", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report["status"] == "blocked"
    assert "protected-code-host" not in report["missing_gates"]
    assert "protected-code-host-pr-lifecycle" not in report["missing_gates"]
    assert "approved-physical-node-lifecycle" in report["missing_gates"]
    assert UPDATE_GATES <= set(report["missing_gates"])
    assert report["physical_update_gates"] == {
        "control_host_update_recovery": False,
        "signed_platform_update_manifest": False,
        "node_canary_and_rollback": False,
    }
    assert report["physical_evidence_key_id"] is None
    assert report["evidence_kinds"]["platform-update"] in {"missing", "simulated"}
    schema = json.loads(
        (ROOT / "schemas/platform-release-evidence.schema.json").read_text()
    )
    jsonschema.validate(report, schema)


def test_code_host_pr_lifecycle_requires_success_for_every_required_check() -> None:
    namespace = runpy.run_path(str(SCRIPT))
    validate = namespace["_code_host_pr_lifecycle_valid"]
    required = ["Ruff", "Generated control clients", "PR contract smoke"]
    valid = {
        "status": "passed",
        "pull_request": 11,
        "merge_commit": "a" * 40,
        "checks": [
            {"name": name, "conclusion": "SUCCESS"} for name in required
        ],
    }
    assert validate(valid, required) is True
    valid["checks"][0]["conclusion"] = "FAILURE"
    assert validate(valid, required) is False


def test_release_verifier_lists_missing_report(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "inventory/reports").mkdir(parents=True)
    (repository / "scripts").mkdir()
    shutil.copy2(SCRIPT, repository / "scripts/verify-platform-release")
    supply = repository / "scripts/verify-supply-chain"
    supply.write_text("#!/bin/sh\nexit 0\n")
    supply.chmod(0o755)
    result = subprocess.run(
        [
            repository / "scripts/verify-platform-release",
            "--root",
            repository,
            "--candidate",
            "1.0.0",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "control-plane-recovery" in json.loads(result.stdout)["missing_gates"]


def test_release_verifier_blocks_malformed_baseline_reports_without_crashing(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _copy_verifier(repository)
    reports = repository / "inventory/reports"
    (reports / "control-plane-recovery.json").write_text("[]\n")
    (reports / "control-plane-scale.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "remaining_release_gates": 7,
            }
        )
    )
    (reports / "platform-lifecycle.json").write_text(
        json.dumps({"schema_version": 1, "status": "passed"})
    )
    (reports / "code-host-protection.json").write_text("[]\n")

    code, report = _verify(repository)

    assert code == 2
    assert report["status"] == "blocked"
    assert "control-plane-recovery:valid-report" in report["missing_gates"]
    assert "control-plane-scale:remaining-gates" in report["missing_gates"]
    assert "protected-code-host" in report["missing_gates"]


@pytest.mark.parametrize(
    ("name", "expected_gate"),
    (
        ("control-plane-recovery.json", "control-plane-recovery:readable"),
        ("platform-update.json", "platform-update:readable"),
        ("code-host-protection.json", "protected-code-host"),
    ),
)
def test_release_verifier_bounds_all_json_report_inputs(
    tmp_path: Path, name: str, expected_gate: str
) -> None:
    repository = tmp_path / "repo"
    _copy_verifier(repository)
    _passing_baseline_reports(repository)
    report = _acceptance_report()
    _write_update_report(repository, report)
    (repository / "inventory/reports" / name).write_bytes(b" " * (1024 * 1024 + 1))

    code, aggregate = _verify(repository)

    assert code == 2
    assert expected_gate in aggregate["missing_gates"]


def _copy_verifier(repository: Path) -> None:
    (repository / "inventory/reports").mkdir(parents=True)
    (repository / "scripts").mkdir()
    shutil.copy2(SCRIPT, repository / "scripts/verify-platform-release")
    supply = repository / "scripts/verify-supply-chain"
    supply.write_text("#!/bin/sh\nexit 0\n")
    supply.chmod(0o755)


def _passing_baseline_reports(repository: Path) -> None:
    reports = repository / "inventory/reports"
    for name in ("control-plane-recovery", "control-plane-scale", "platform-lifecycle"):
        (reports / f"{name}.json").write_text(
            json.dumps({"schema_version": 1, "status": "passed"})
        )
    (reports / "code-host-protection.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "protected_branch": True,
                "required_checks": ["tests"],
            }
        )
    )


def _acceptance_report() -> dict[str, object]:
    result = subprocess.run(
        [ACCEPT_UPDATE, "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_update_report(repository: Path, report: dict[str, object]) -> None:
    content = dict(report)
    content.pop("digest", None)
    import hashlib

    content["digest"] = "sha256:" + hashlib.sha256(_canonical(content)).hexdigest()
    (repository / "inventory/reports/platform-update.json").write_bytes(
        _canonical(content)
    )


def _write_workload_evidence(repository: Path) -> None:
    reports = repository / "inventory/reports"
    acceptance: dict[str, object] = {
        "schema_version": 1,
        "report_type": "vonk-forge-workload-package-acceptance",
        "status": "passed",
        "evidence_kind": "simulated",
        "physical_nodes_exercised": False,
        "unknown_family_without_agent_update": True,
        "agent_digest_unchanged": True,
        "release_two_activated": True,
        "offline_release_one_rollback": True,
        "unsigned_release_rejected": True,
        "unapproved_release_rejected": True,
        "ssh_calls": 0,
        "agent_update_calls": 0,
    }
    failure: dict[str, object] = {
        "schema_version": 1,
        "report_type": "vonk-forge-workload-package-failure-matrix",
        "status": "passed",
        "evidence_kind": "simulated",
        "failure_matrix": True,
        "physical_nodes_exercised": False,
        "ssh_calls": 0,
        "agent_update_calls": 0,
        "restart_recovery": "passed",
        "gc_restart_recovery": "passed",
        "concurrent_identical_downloads": "one-fetch-many-consumers",
        "cases": [
            {
                "family_id": "synthetic-stack",
                "release_digest": "sha256:" + "a" * 64,
                "node_id": "spk_" + "1" * 32,
                "fence": "fence-1",
                "reason_code": "transport-unavailable",
                "disposition": "safe-to-retry",
            }
        ],
    }
    for filename, report in (
        ("workload-package-acceptance.json", acceptance),
        ("workload-package-failure-matrix.json", failure),
    ):
        report["digest"] = "sha256:" + hashlib.sha256(
            _canonical(report)
        ).hexdigest()
        (reports / filename).write_bytes(_canonical(report))


def _write_physical_evidence(repository: Path, report: dict[str, object]) -> Path:
    mapping = {
        "control_host_update_recovery": (
            "platform-update-control-host-recovery.json",
            "control-host-update-recovery",
        ),
        "signed_platform_update_manifest": (
            "platform-update-signed-manifest.json",
            "signed-platform-update-manifest",
        ),
        "node_canary_and_rollback": (
            "platform-update-node-canary-rollback.json",
            "node-canary-and-rollback",
        ),
    }
    candidate = "1.0.0"
    release_digest = "sha256:" + "b" * 64
    run_id = "50000000-0000-4000-8000-000000000005"
    report.update(
        {
            "candidate": candidate,
            "release_digest": release_digest,
            "run_id": run_id,
        }
    )
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("22" * 32))
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = hashlib.sha256(public).hexdigest()
    public_document = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "public_key": public.hex(),
        "schema_version": 1,
    }
    public_path = repository / "trusted-physical-evidence-public.json"
    public_path.write_bytes(_canonical(public_document))

    for gate, (name, evidence_type) in mapping.items():
        claims: dict[str, object]
        if gate == "signed_platform_update_manifest":
            claims = {
                "agent_payload_sha256": {"linux-arm64": "c" * 64},
                "build_digest": "sha256:" + "e" * 64,
                "platform_target_name": (
                    f"platform/releases/{candidate}/" + "b" * 64 + ".json"
                ),
                "platform_target_sha256": "b" * 64,
                "tuf_targets_version": 7,
            }
        elif gate == "control_host_update_recovery":
            claims = {
                "candidate_generation": "gen-" + "b" * 24,
                "from_generation": "gen-" + "a" * 24,
                "recovered_generation": "gen-" + "b" * 24,
                "recovery_evidence_sha256": "e" * 64,
                "rollback_evidence_sha256": "f" * 64,
                "rolled_back_generation": "gen-" + "a" * 24,
            }
        else:
            claims = {
                "architecture": "linux-arm64",
                "canary_node": "spk_" + "1" * 32,
                "from_agent_sha256": "a" * 64,
                "from_platform_version": "0.9.0",
                "from_slot": "A",
                "rollback_evidence_sha256": "f" * 64,
                "rollback_slot": "A",
                "ssh_used_for_standard_path": False,
                "supervisor_generation": 2,
                "target_agent_sha256": "c" * 64,
                "target_build_digest": "sha256:" + "e" * 64,
                "target_platform_version": candidate,
                "target_slot": "B",
                "transport": "outbound-mtls-agent-channel",
            }
        evidence = {
            "candidate": candidate,
            "claims": claims,
            "details_sha256": "c" * 64,
            "evidence_kind": "physical",
            "evidence_type": evidence_type,
            "observed_at": "2026-08-06T01:00:00Z",
            "physical_exercised": True,
            "release_digest": release_digest,
            "run_id": run_id,
            "schema_version": 1,
            "status": "passed",
        }
        envelope = {
            "evidence": evidence,
            "schema_version": 1,
            "signature": {
                "algorithm": "ed25519",
                "key_id": key_id,
                "value": private.sign(_canonical(evidence)).hex(),
            },
        }
        content = _canonical(envelope)
        (repository / "inventory/reports" / name).write_bytes(content)
        report["physical_evidence"][gate]["evidence_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
    return public_path


def _verify(
    repository: Path,
    *,
    candidate: str = "1.0.0",
    physical_key: Path | None = None,
) -> tuple[int, dict[str, object]]:
    command = [
        repository / "scripts/verify-platform-release",
        "--root",
        repository,
        "--candidate",
        candidate,
        "--json",
    ]
    if physical_key is not None:
        command.extend(["--physical-evidence-public-key", physical_key])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def _verify_with_injected_trusted_key(
    repository: Path,
    *,
    candidate: str,
    physical_key: Path,
) -> tuple[int, dict[str, object]]:
    namespace = runpy.run_path(str(repository / "scripts/verify-platform-release"))
    verify = namespace["verify"]
    public_document = json.loads(physical_key.read_text())
    public = ed25519.Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(public_document["public_key"])
    )
    verify.__globals__["_physical_evidence_key"] = lambda _path: (
        public,
        public_document["key_id"],
    )
    report, missing = verify(repository, candidate, physical_key)
    return (2 if missing else 0), report


def test_simulated_update_report_never_satisfies_physical_release_gates(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _copy_verifier(repository)
    _passing_baseline_reports(repository)
    report = _acceptance_report()
    for gate in report["physical_evidence"].values():
        gate["exercised"] = True
        gate["evidence_sha256"] = "a" * 64
    _write_update_report(repository, report)

    code, aggregate = _verify(repository)

    assert code == 2
    assert UPDATE_GATES <= set(aggregate["missing_gates"])
    assert not any(aggregate["physical_update_gates"].values())


def test_workload_failure_helper_emits_report_accepted_by_release_verifier(
    tmp_path: Path,
) -> None:
    helper = ROOT / "scripts/accept-workload-package-failures"
    if not helper.exists():
        pytest.skip("W19 failure evidence helper is not present yet")
    output = tmp_path / "workload-package-failure-matrix.json"
    completed = subprocess.run(
        [helper, "--output", str(output), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    namespace = runpy.run_path(str(SCRIPT))
    # The helper writes to its requested path; copy it into the verifier's
    # canonical inventory location before validating the real artifact.
    inventory = tmp_path.parent / "inventory/reports"
    inventory.mkdir(parents=True)
    target = inventory / output.name
    target.write_bytes(output.read_bytes())
    report, digest = namespace["_read_workload_report"](
        tmp_path.parent, "workload-package-failure-matrix"
    )
    assert report is not None
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert namespace["_failure_matrix_valid"](report)


def test_unknown_workload_acceptance_helper_runs_real_e2e_and_emits_canonical_report(
    tmp_path: Path,
) -> None:
    helper = ROOT / "scripts/accept-workload-packages"
    output = tmp_path / "workload-package-acceptance.json"
    completed = subprocess.run(
        [helper, "--mode", "simulated", "--output", str(output), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text())
    namespace = runpy.run_path(str(SCRIPT))
    assert namespace["_workload_acceptance_valid"](report)
    assert report["test_command"]
    assert "test_unknown_workload_package_e2e.py" in report["test_command"]
    assert report["physical_nodes_exercised"] is False


def test_physical_update_evidence_must_be_complete_and_content_addressed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _copy_verifier(repository)
    _passing_baseline_reports(repository)
    _write_workload_evidence(repository)
    report = _acceptance_report()
    report["evidence_kind"] = "physical"
    report["remaining_release_gates"] = []
    for gate in report["physical_evidence"].values():
        gate["exercised"] = True
        gate["evidence_sha256"] = "a" * 64
    _write_update_report(repository, report)

    code, aggregate = _verify(repository)

    assert code == 2
    assert UPDATE_GATES <= set(aggregate["missing_gates"])
    assert "platform-update:trusted-physical-evidence-key" in aggregate["missing_gates"]

    physical_key = _write_physical_evidence(repository, report)
    _write_update_report(repository, report)
    code, aggregate = _verify(repository, physical_key=physical_key)

    if os.geteuid() != 0:
        assert code == 2
        assert (
            "platform-update:trusted-physical-evidence-key"
            in aggregate["missing_gates"]
        )
        code, aggregate = _verify_with_injected_trusted_key(
            repository,
            candidate="1.0.0",
            physical_key=physical_key,
        )

    assert code == 0
    assert aggregate["status"] == "passed"
    assert aggregate["missing_gates"] == []
    assert all(aggregate["physical_update_gates"].values())
    assert (
        aggregate["physical_evidence_key_id"]
        == hashlib.sha256(
            bytes.fromhex(json.loads(physical_key.read_text())["public_key"])
        ).hexdigest()
    )

    spark_path = (
        repository / "inventory/reports/platform-update-node-canary-rollback.json"
    )
    node = json.loads(spark_path.read_text())
    node["evidence"]["claims"]["target_agent_sha256"] = "d" * 64
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("22" * 32))
    node["signature"]["value"] = private.sign(_canonical(node["evidence"])).hex()
    spark_path.write_bytes(_canonical(node))
    update_path = repository / "inventory/reports/platform-update.json"
    update = json.loads(update_path.read_text())
    update["physical_evidence"]["node_canary_and_rollback"]["evidence_sha256"] = (
        hashlib.sha256(spark_path.read_bytes()).hexdigest()
    )
    _write_update_report(repository, update)
    code, aggregate = _verify_with_injected_trusted_key(
        repository,
        candidate="1.0.0",
        physical_key=physical_key,
    )
    assert code == 2
    assert aggregate["physical_update_gates"]["signed_platform_update_manifest"]
    assert not aggregate["physical_update_gates"]["node_canary_and_rollback"]

    physical_key = _write_physical_evidence(repository, report)
    _write_update_report(repository, report)

    manifest_path = (
        repository / "inventory/reports/platform-update-signed-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["evidence"]["claims"]["platform_target_name"] = (
        "platform/releases/1.0.0/" + "d" * 64 + ".json"
    )
    manifest["evidence"]["claims"]["platform_target_sha256"] = "d" * 64
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("22" * 32))
    manifest["signature"]["value"] = private.sign(
        _canonical(manifest["evidence"])
    ).hex()
    manifest_path.write_bytes(_canonical(manifest))
    update_path = repository / "inventory/reports/platform-update.json"
    update = json.loads(update_path.read_text())
    update["physical_evidence"]["signed_platform_update_manifest"][
        "evidence_sha256"
    ] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write_update_report(repository, update)
    code, aggregate = _verify_with_injected_trusted_key(
        repository,
        candidate="1.0.0",
        physical_key=physical_key,
    )
    assert code == 2
    assert "signed-platform-update-manifest-evidence" in aggregate["missing_gates"]

    physical_key = _write_physical_evidence(repository, report)
    _write_update_report(repository, report)

    code, aggregate = _verify_with_injected_trusted_key(
        repository,
        candidate="1.0.1",
        physical_key=physical_key,
    )
    assert code == 2
    assert "platform-update:candidate-binding" in aggregate["missing_gates"]

    mixed_path = (
        repository / "inventory/reports/platform-update-control-host-recovery.json"
    )
    mixed = json.loads(mixed_path.read_text())
    mixed["evidence"]["run_id"] = "60000000-0000-4000-8000-000000000006"
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("22" * 32))
    mixed["signature"]["value"] = private.sign(_canonical(mixed["evidence"])).hex()
    mixed_path.write_bytes(_canonical(mixed))
    update_path = repository / "inventory/reports/platform-update.json"
    update = json.loads(update_path.read_text())
    update["physical_evidence"]["control_host_update_recovery"]["evidence_sha256"] = (
        hashlib.sha256(mixed_path.read_bytes()).hexdigest()
    )
    _write_update_report(repository, update)
    code, aggregate = _verify_with_injected_trusted_key(
        repository,
        candidate="1.0.0",
        physical_key=physical_key,
    )
    assert code == 2
    assert "physical-control-host-update-recovery" in aggregate["missing_gates"]

    path = repository / "inventory/reports/platform-update.json"
    tampered = json.loads(path.read_text())
    tampered["fleet"]["scheduled_node_count"] += 1
    path.write_bytes(_canonical(tampered))
    code, aggregate = _verify_with_injected_trusted_key(
        repository,
        candidate="1.0.0",
        physical_key=physical_key,
    )
    assert code == 2
    assert "platform-update:content-addressed" in aggregate["missing_gates"]
