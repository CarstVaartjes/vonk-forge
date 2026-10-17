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

import pytest
from vonk_agent_protocol import AgentFailureResult, AgentProtocolError, AgentResult
from vonk_agent_protocol.optional_evidence import HOST_PATH_MARKER, redact_host_paths
from vonk_agent_protocol.reason_codes import AgentEvidenceCode

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def _vector(name: str) -> dict[str, object]:
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
    document["result"]["evidence"]["diagnostic"] = "upload of /outputs/a.png refused"  # type: ignore[index]

    accepted = AgentResult.model_validate(document)

    assert AgentEvidenceCode.RESULT_HOST_PATH_REDACTED in accepted.evidence_warnings
    ending = accepted.stored_result().ending
    assert ending.diagnostic == "upload of <host-path> refused"


@pytest.mark.parametrize("parse", [AgentResult.model_validate, AgentResult.parse])
def test_a_path_in_a_field_the_controller_acts_on_still_refuses(
    parse: Callable[[object], AgentResult],
) -> None:
    document = copy.deepcopy(_vector("recipe-job-run-unknown-receipt-v1.json"))
    receipt = document["result"]["receipt"]  # type: ignore[index]
    receipt["output_manifest"]["files"][0]["name"] = "/etc/passwd"

    with pytest.raises((AgentProtocolError, ValueError)) as refused:
        parse(document)
    assert "output_manifest" in str(refused.value)


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("open /opt/a.json failed", "open <host-path> failed"),
        ("path='/opt/a.json':3", "path='<host-path>':3"),
        ("endpoint=/agent/result", "endpoint=<host-path>"),
        ("C:\\cache\\x", "C:<host-path>"),
        ("no separator here", "no separator here"),
    ],
)
def test_redaction_replaces_whole_path_tokens(text: str, redacted: str) -> None:
    assert redact_host_paths(text) == redacted
