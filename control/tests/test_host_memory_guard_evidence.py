"""A guard kill's readings survive the Controller's diagnostics sanitizer."""

from __future__ import annotations

import json
from importlib.resources import files

from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_control.failure_evidence import sanitize_diagnostics


def _facts(summary: str) -> list[dict[str, str]]:
    """The summary's ``name=value`` facts, as the agent keeps them."""
    return [
        {"name": name, "value": value}
        for name, _, value in (
            token.partition("=") for token in summary.split() if "=" in token
        )
    ]


def test_the_shared_guard_vector_keeps_every_reading_through_the_controller() -> None:
    # Break caught (#1936 review): recent samples joined with `-` formed a
    # 40+ character run that the opaque-value rule redacted.
    vector = json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/host-memory-guard-summary-v1.json")
        .read_text(encoding="utf-8")
    )
    facts = _facts(vector["summary"])
    assert len(facts) <= 8
    diagnostics = FailureDiagnostics.model_validate(
        {
            "schema_version": 1,
            "collected_at": "2026-10-28T12:00:00+00:00",
            "phase": "recipe.start",
            "category": "capacity",
            "stdout": {
                "text": "",
                "truncated": False,
                "dropped_bytes": 0,
                "dropped_lines": 0,
            },
            "stderr": {
                "text": "",
                "truncated": False,
                "dropped_bytes": 0,
                "dropped_lines": 0,
            },
            "versions": [],
            "sandbox": [],
            "storage": [],
            "preflight": facts,
            "collector_errors": [],
        }
    )
    sanitized = sanitize_diagnostics(diagnostics.model_dump(mode="json"))
    assert [p.model_dump() for p in sanitized.preflight] == facts


def test_every_guard_trigger_at_worst_case_values_survives_the_controller() -> None:
    # Break caught (#1936 third review): a 40+ character fact such as
    # ``trigger=sustained_memavailable_exhaustion`` is redacted as opaque.
    vector = json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/host-memory-guard-summary-v1.json")
        .read_text(encoding="utf-8")
    )
    assert len(vector["worst_case"]) == 6
    for summary in vector["worst_case"]:
        facts = _facts(summary)
        assert len(facts) == 8
        diagnostics = FailureDiagnostics.model_validate(
            {
                "schema_version": 1,
                "collected_at": "2026-12-31T23:59:59+00:00",
                "phase": "recipe.start",
                "category": "capacity",
                "stdout": {
                    "text": "",
                    "truncated": False,
                    "dropped_bytes": 0,
                    "dropped_lines": 0,
                },
                "stderr": {
                    "text": "",
                    "truncated": False,
                    "dropped_bytes": 0,
                    "dropped_lines": 0,
                },
                "versions": [],
                "sandbox": [],
                "storage": [],
                "preflight": facts,
                "collector_errors": [],
            }
        )
        sanitized = sanitize_diagnostics(diagnostics.model_dump(mode="json"))
        assert [p.model_dump() for p in sanitized.preflight] == facts, summary
