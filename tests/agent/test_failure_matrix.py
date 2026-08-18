from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent_protocol/src"))
sys.path.insert(0, str(ROOT / "agent/src"))

_simulator = import_module("vonk_agent.simulator")
canonical_report = _simulator.canonical_report
simulate_agent_lifecycle = _simulator.simulate_agent_lifecycle
LINUX_AGENT_STATE_RUNTIME = pytest.mark.skipif(
    sys.platform != "linux",
    reason="secure Vonk Forge agent state runtime requires Linux descriptor traversal",
)


@LINUX_AGENT_STATE_RUNTIME
@pytest.mark.parametrize("nodes", [1, 16])
def test_failure_matrix_preserves_agent_lifecycle_invariants(nodes: int) -> None:
    report = simulate_agent_lifecycle(nodes=nodes, seed=20260803)

    assert report["schema_version"] == 1
    assert report["evidence_kind"] == "simulated"
    assert report["environment"] == "deterministic-in-memory-transport"
    assert report["physical_nodes_exercised"] is False
    assert report["node_count"] == nodes
    assert report["seed"] == 20260803
    assert report["faults"] == {
        "bad-artifact": {
            "artifact_rejections": nodes,
            "candidate_activations": 0,
            "candidate_cleanups": nodes,
            "evidence_kind": "simulated",
            "injected": nodes,
            "pending_candidates_after_fault": 0,
            "rollbacks": 0,
            "safe_outcomes": nodes,
            "transition_sequence": [
                "candidate-staged:B:generation-1",
                "artifact-validation-rejected:B:generation-1",
                "candidate-cleared:B:generation-1",
            ],
            "transition_sequence_mismatches": 0,
        },
        "bad-certificate": {
            "durable_mutations": 0,
            "evidence_kind": "simulated",
            "injected": nodes,
            "rejections": nodes,
        },
        "crash": {
            "evidence_kind": "simulated",
            "injected": nodes,
            "recoveries": nodes,
        },
        "disconnect": {
            "evidence_kind": "simulated",
            "injected": nodes,
            "recoveries": nodes,
        },
        "failed-activation": {
            "activation_failures": nodes,
            "candidate_activations": nodes,
            "candidate_cleanups": nodes,
            "evidence_kind": "simulated",
            "injected": nodes,
            "pending_candidates_after_fault": 0,
            "readiness_failures": nodes,
            "rollbacks": nodes,
            "safe_outcomes": nodes,
            "transition_sequence": [
                "candidate-staged:B:generation-1",
                "artifact-validated:B:generation-1",
                "activation-attempted:A->B:generation-1",
                "active-switched:A->B:generation-1",
                "readiness-failed:B:generation-1",
                "active-restored:B->A:generation-1",
                "candidate-cleared:B:generation-1",
            ],
            "transition_sequence_mismatches": 0,
        },
        "stale-fence": {
            "claim_rejections": nodes,
            "durable_mutations": 0,
            "evidence_kind": "simulated",
            "injected": nodes,
            "result_rejections": nodes,
        },
    }

    invariants = report["invariants"]
    assert invariants == {
        "artifact_rejections_without_activation": nodes,
        "bad_update_rollbacks": nodes,
        "bad_update_safe_outcomes": nodes * 2,
        "crash_recoveries": nodes,
        "cross_node_claims_accepted": 0,
        "duplicate_mutations": 0,
        "reconnect_recoveries": nodes,
        "stale_results_accepted": 0,
    }
    assert report["status"] == "passed"


@LINUX_AGENT_STATE_RUNTIME
@pytest.mark.parametrize(
    ("fault", "counter", "regressed_value"),
    [
        ("bad-artifact", "rollbacks", 1),
        ("bad-certificate", "rejections", 0),
        ("bad-certificate", "durable_mutations", 1),
        ("failed-activation", "rollbacks", 0),
        ("stale-fence", "claim_rejections", 0),
        ("stale-fence", "durable_mutations", 1),
    ],
)
def test_status_rejects_incomplete_or_mutating_security_faults(
    fault: str, counter: str, regressed_value: int
) -> None:
    evaluator = _simulator.lifecycle_evidence_passes
    report = simulate_agent_lifecycle(nodes=1, seed=20260803)
    regressed = deepcopy(report)
    regressed["faults"][fault][counter] = regressed_value

    assert evaluator(regressed) is False


@LINUX_AGENT_STATE_RUNTIME
def test_simulation_is_seeded_and_canonical() -> None:
    first = simulate_agent_lifecycle(nodes=16, seed=918273)
    repeated = simulate_agent_lifecycle(nodes=16, seed=918273)
    different = simulate_agent_lifecycle(nodes=16, seed=918274)

    assert first == repeated
    assert first != different
    encoded = canonical_report(first)
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == first
    assert encoded == canonical_report(json.loads(encoded))


@LINUX_AGENT_STATE_RUNTIME
def test_acceptance_cli_emits_only_canonical_simulated_evidence() -> None:
    completed = subprocess.run(
        [ROOT / "scripts/accept-agent-lifecycle", "--nodes", "16", "--json"],
        check=True,
        capture_output=True,
    )
    report = json.loads(completed.stdout)

    assert completed.stderr == b""
    assert completed.stdout == canonical_report(report)
    assert report["evidence_kind"] == "simulated"
    assert report["physical_nodes_exercised"] is False
    assert report["status"] == "passed"


def test_acceptance_cli_rejects_accidental_oversized_fleet_before_simulation() -> None:
    completed = subprocess.run(
        [ROOT / "scripts/accept-agent-lifecycle", "--nodes", "257", "--json"],
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"exceeds the default safety threshold of 256" in completed.stderr


def test_large_fleet_override_is_explicitly_parsed_and_removes_only_cli_guard() -> None:
    parser = _simulator.acceptance_argument_parser()
    args = parser.parse_args(["--nodes", "17", "--allow-large-fleet"])

    assert args.allow_large_fleet is True
    assert args.nodes == 17
    _simulator.validate_cli_fleet_size(
        nodes=args.nodes,
        allow_large_fleet=args.allow_large_fleet,
        safety_threshold=16,
    )
    with pytest.raises(ValueError, match="safety threshold"):
        _simulator.validate_cli_fleet_size(
            nodes=args.nodes,
            allow_large_fleet=False,
            safety_threshold=16,
        )
