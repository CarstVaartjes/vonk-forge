"""The Rust agent's own messages are accepted by ingress as they are sent.

Each vector here is produced by the agent's test
(``executor/shared_vectors_tests.rs``): an upgrade awaiting its new identity,
an upgrade whose package the helper refused, and the run observation that
carries an exited workload's inspected output. None needs a redaction or loses
an evidence group on the way in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vonk_agent_protocol import AgentFailureResult, AgentResult, OutcomeUnknown
from vonk_agent_protocol.recipe_observations import RecipeRunObservationsWire

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def _vector(name: str) -> Any:
    return json.loads((VECTORS / name).read_text())


def test_an_upgrade_awaiting_its_identity_is_accepted_whole() -> None:
    accepted = AgentResult.model_validate(
        _vector("agent-upgrade-awaiting-identity-v1.json")
    )

    assert accepted.evidence_warnings == ()
    assert isinstance(accepted.result, OutcomeUnknown)
    assert accepted.result.wait_reason == "agent-upgrade-awaiting-identity"


def test_a_refused_upgrade_package_is_accepted_with_its_detail() -> None:
    accepted = AgentResult.model_validate(_vector("agent-upgrade-refused-v1.json"))

    assert accepted.evidence_warnings == ()
    stored = accepted.stored_result()
    assert isinstance(stored, AgentFailureResult)
    assert stored.error_code == "agent_upgrade_failed"
    assert stored.helper_error_code == "package_verification_failed"
    # The reason is the typed refusal; the helper's words are the detail.
    assert stored.reason is not None and "/" not in stored.reason
    assert stored.diagnostics is not None
    assert "/var/lib/vonk/incoming" in stored.diagnostics.stderr.text


def test_an_inspected_exited_workload_observation_is_accepted() -> None:
    report = RecipeRunObservationsWire.parse(
        _vector("recipe-run-observation-exited-v1.json")
    )

    (run,) = report.runs
    assert run.process_running is False
    assert run.failure_diagnostics is not None
    assert "fused_moe.json" in run.failure_diagnostics.stderr.text
    facts = {item.name: item.value for item in run.failure_diagnostics.preflight}
    assert facts["exit_cause"] == "python_exception"
