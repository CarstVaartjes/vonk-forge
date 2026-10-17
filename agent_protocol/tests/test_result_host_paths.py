"""A host path in a result's prose is redacted; the result is never refused for it.

The shared vector is the body the Rust agent produces for a start whose workload
raised an exception naming absolute paths: the body every live Spark had
refused with ``422 filesystem path values are not allowed``. Prose is only
displayed, so its paths become a typed marker. Fields the Controller acts on
(manifest names, identities) stay strict: a path there still refuses.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from vonk_agent_protocol import AgentFailureResult, AgentProtocolError, AgentResult
from vonk_agent_protocol.optional_evidence import HOST_PATH_MARKER, redact_host_paths
from vonk_agent_protocol.reason_codes import AgentEvidenceCode

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def _vector(name: str) -> Any:
    return json.loads((VECTORS / name).read_text())


def test_a_start_failure_naming_host_paths_is_accepted_with_its_core() -> None:
    accepted = AgentResult.model_validate(
        _vector("recipe-start-failed-host-paths-v1.json")
    )

    assert accepted.evidence_warnings == (AgentEvidenceCode.RESULT_HOST_PATH_REDACTED,)
    stored = accepted.stored_result()
    assert isinstance(stored, AgentFailureResult)
    assert stored.error_code == "recipe_start_failed"
    # The sentence keeps what it says; only the path is gone.
    for text in (stored.reason, stored.diagnostic):
        assert text is not None
        assert "FileNotFoundError" in text and HOST_PATH_MARKER in text
        assert "/" not in text and "\\" not in text
    # The captured streams are bounded evidence and stay as the agent sent them.
    assert stored.diagnostics is not None
    assert "/opt/vonk/prewarm" in stored.diagnostics.stderr.text


def test_a_host_path_in_a_receipted_ending_is_redacted_where_it_is() -> None:
    document = copy.deepcopy(_vector("recipe-job-run-unknown-receipt-v1.json"))
    document["result"]["evidence"]["diagnostic"] = "upload of /outputs/a.png refused"

    accepted = AgentResult.model_validate(document)

    assert AgentEvidenceCode.RESULT_HOST_PATH_REDACTED in accepted.evidence_warnings
    ending = accepted.stored_result().ending
    assert ending.diagnostic == "upload of <host-path> refused"


@pytest.mark.parametrize("parse", [AgentResult.model_validate, AgentResult.parse])
def test_a_path_in_a_field_the_controller_acts_on_still_refuses(
    parse: Callable[[object], AgentResult],
) -> None:
    document = copy.deepcopy(_vector("recipe-job-run-unknown-receipt-v1.json"))
    receipt = document["result"]["receipt"]
    receipt["output_manifest"]["files"][0]["name"] = "/etc/passwd"

    with pytest.raises((AgentProtocolError, ValueError)) as refused:
        parse(document)
    assert "output_manifest" in str(refused.value)


def test_redaction_follows_the_shared_rule() -> None:
    """The agent redacts by the same vector before it sends."""

    vector = _vector("host-path-redaction-v1.json")
    assert vector["marker"] == HOST_PATH_MARKER
    for case in vector["cases"]:
        assert redact_host_paths(case["text"]) == case["redacted"], case["text"]
        assert "/" not in case["redacted"] and "\\" not in case["redacted"]
        assert len(case["redacted"]) <= max(
            len(case["text"]), vector["shortest_prose_bound"]
        )
