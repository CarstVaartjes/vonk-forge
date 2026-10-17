"""A Controller denial the agent reports keeps its evidence through ingress.

The shared vector holds the result bodies the agent sends for every route by
which a Controller denial reaches result evidence; the agent's Rust suite
proves the vector is its real output. Here the Controller's own result
validation parses each one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from vonk_agent_protocol import AgentEvidenceCode, AgentFailureResult, AgentResult
from vonk_agent_protocol.optional_evidence import redact_host_paths

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"
CASES: list[dict[str, Any]] = json.loads(
    (VECTORS / "controller-denial-results-v1.json").read_text()
)["cases"]


def _ingest(case: dict[str, Any]) -> tuple[AgentResult, AgentFailureResult]:
    message = AgentResult.parse(
        {"fence": str(uuid4()), "state": case["state"], "result": case["result"]}
    )
    stored = message.stored_result()
    assert isinstance(stored, AgentFailureResult)
    return message, stored


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_controller_denial_keeps_its_diagnostic_and_stage(
    case: dict[str, Any],
) -> None:
    message, stored = _ingest(case)

    assert message.evidence_warnings == ()
    assert stored.diagnostic == case["result"]["evidence"]["diagnostic"]
    assert stored.stage == case["result"]["evidence"]["stage"]


def test_a_route_rendered_as_a_url_path_is_redacted_in_the_diagnostic() -> None:
    # The form the agent sent before: the check above fails on it.
    case = copy.deepcopy(
        next(
            case for case in CASES if case["name"] == "distribution-assignment-refused"
        )
    )
    evidence = case["result"]["evidence"]
    old = evidence["diagnostic"].replace(
        "endpoint=agent.distribution.assignment",
        "endpoint=/agent/distribution/assignment",
    )
    assert old != evidence["diagnostic"]
    evidence["diagnostic"] = old

    message, stored = _ingest(case)

    assert message.evidence_warnings == (AgentEvidenceCode.RESULT_HOST_PATH_REDACTED,)
    assert stored.diagnostic == redact_host_paths(old)
    assert "endpoint=" in stored.diagnostic and "/" not in stored.diagnostic
    assert stored.stage == evidence["stage"]
