from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/accept-platform-update"
PHYSICAL_GATES = {
    "physical-control-host-update-recovery",
    "physical-node-canary-rollback",
    "signed-platform-update-manifest-evidence",
}


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _run(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_admin_simulation_confirms_the_exact_versioned_target() -> None:
    target_name = "platform/releases/2.0.0/" + "d" * 64 + ".json"
    plan_digest = "sha256:" + "e" * 64
    program = "\n".join(
        (
            "import json, os, runpy",
            'os.environ["VONK_PLATFORM_UPDATE_LOCKED_ENV"] = "1"',
            f"module = runpy.run_path({str(SCRIPT)!r})",
            f"print(json.dumps(module['_admin_interface_scenario']({plan_digest!r}, {target_name!r})))",
        )
    )
    result = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            ROOT / "control",
            "--with-editable",
            ROOT,
            "python",
            "-c",
            program,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "api_apply_status": 202,
        "api_rollout_plan_digest": plan_digest,
        "api_resume_status": 202,
        "api_skew_status": 200,
        "cli_commands": ["skew", "plan", "apply", "status"],
        "confirmation_required": True,
        "platform_target_name": target_name,
        "skew_platform_target_name": target_name,
        "skew_release_digest": "sha256:" + "d" * 64,
        "skew_target_sha256": "d" * 64,
        "skew_tuf_targets_version": 7,
    }


def test_host_simulation_recovers_an_exact_versioned_generation(
    tmp_path: Path,
) -> None:
    program = "\n".join(
        (
            "import json, os, runpy",
            "from pathlib import Path",
            'os.environ["VONK_PLATFORM_UPDATE_LOCKED_ENV"] = "1"',
            f"module = runpy.run_path({str(SCRIPT)!r})",
            f"print(json.dumps(module['_host_generation_scenario'](Path({str(tmp_path)!r}))))",
        )
    )
    result = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            ROOT / "control",
            "--with-editable",
            ROOT,
            "python",
            "-c",
            program,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["crash_recovered"] is True
    assert report["selected_generation"] == report["old_generation"]
    assert report["platform_target_name"] == (
        "platform/releases/2.0.0/" + report["platform_target_sha256"] + ".json"
    )
    assert report["tuf_targets_version"] == 7


def test_acceptance_exercises_the_staged_update_contract_without_ssh() -> None:
    result = _run("--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["report_type"] == "vonk-forge-platform-update"
    assert report["status"] == "passed"
    assert report["evidence_kind"] == "simulated"
    assert report["standard_path"] == "outbound-mtls-agent-channel"
    assert report["ssh_used_for_standard_path"] is False
    assert report["interfaces_exercised"] == [
        "ControlUpgrade",
        "PlatformRelease",
        "UpdateAuthorizationAuthority",
        "UpdateOrchestrator",
        "UpdatePlanner",
        "VersionSkewAnalyzer",
        "control API update route contract with simulated service",
        "vonkctl update command contract with simulated client",
    ]
    assert report["scenarios"] == {
        "canary_failure_pause": "passed",
        "compatible_rolling_update": "passed",
        "admin_confirmation_route_contract": "passed",
        "control_host_old_new_rollback_recovery": "passed",
        "final_fleet_and_model_verification": "passed",
        "incompatible_mutation_blocking": "passed",
        "nas_newer_prompt_without_mutation": "passed",
        "offline_node_pending": "passed",
        "resume_after_administrator_approval": "passed",
    }
    assert report["scenario_evidence"] == {name: True for name in report["scenarios"]}
    assert report["fleet"]["scheduled_node_count"] == 16
    assert report["fleet"]["accepted_node_count"] == 16
    assert report["fleet"]["orchestrator_rollout_state"] == "partial"
    assert report["fleet"]["route_withdrawal_count"] == 17
    assert report["fleet"]["route_restoration_count"] == 17
    assert report["fleet"]["canary_node"].startswith("spk_")
    assert len(report["fleet"]["offline_pending"]) == 1
    assert len(report["fleet"]["incompatible"]) == 1
    assert report["fleet"]["signed_plan_digest"].startswith("sha256:")
    assert len(report["fleet"]["signed_authorization_key_id"]) == 64
    assert report["fleet"]["signed_authorization_receipt_sha256"].startswith("sha256:")
    assert report["fleet"]["canary_failure_trace"] == [
        "routes-withdrawn",
        "canary-update-failed-after-slot-B",
        "rollout-paused",
        "operator-rollback-dispatched",
        "slot-A-readiness-proven",
        "routes-restored",
        "administrator-approved-resume",
        "sixteen-nodes-accepted",
    ]
    assert "agent_update" not in report["fleet"]
    assert report["fleet"]["final_model_probe"] == {
        "health": "healthy",
        "repository_revision": "f" * 40,
        "request_count": 2,
        "requested_paths": ["/health", "/v1/models"],
        "route_status": 200,
    }
    assert report["host"]["authorized_predecessor"] is True
    assert report["host"]["crash_recovered"] is True
    assert report["admin_interfaces"] == {
        "api_apply_status": 202,
        "api_rollout_plan_digest": report["fleet"]["signed_plan_digest"],
        "api_resume_status": 202,
        "api_skew_status": 200,
        "cli_commands": ["skew", "plan", "apply", "status"],
        "confirmation_required": True,
        "platform_target_name": report["fleet"]["platform_target_name"],
        "skew_platform_target_name": report["fleet"]["platform_target_name"],
        "skew_release_digest": report["fleet"]["release_digest"],
        "skew_target_sha256": report["fleet"]["platform_target_sha256"],
        "skew_tuf_targets_version": report["fleet"]["tuf_targets_version"],
    }


def test_simulator_evidence_is_content_addressed_and_cannot_claim_physical_gates() -> (
    None
):
    result = _run("--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    digest = report.pop("digest")
    assert digest == "sha256:" + hashlib.sha256(_canonical(report)).hexdigest()
    assert report["physical_evidence"] == {
        "control_host_update_recovery": {
            "evidence_sha256": None,
            "exercised": False,
        },
        "signed_platform_update_manifest": {
            "evidence_sha256": None,
            "exercised": False,
        },
        "node_canary_and_rollback": {
            "evidence_sha256": None,
            "exercised": False,
        },
    }
    assert set(report["remaining_release_gates"]) == PHYSICAL_GATES


def test_acceptance_output_is_canonical_and_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_run = _run("--output", first)
    second_run = _run("--output", second)

    assert first_run.returncode == second_run.returncode == 0
    assert first_run.stdout == second_run.stdout == ""
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")
    assert _canonical(json.loads(first.read_bytes())) == first.read_bytes()
