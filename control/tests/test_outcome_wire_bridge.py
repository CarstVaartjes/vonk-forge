"""The lifecycle vocabulary and the outcome envelope cross the Rust protocol unchanged.

Every document is produced by the Pydantic contract, parsed by the generated Rust
types (the Rust agent's own), and printed back; the Python contract must read what
Rust printed as the same document.  A word or shape that only one side knows, or a
document one side accepts and the other refuses, fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
from enum import Enum
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel
from vonk_agent_protocol import (
    AgentResult,
    ErrorCatalog,
    LifecycleVocabulary,
    OutcomeCatalog,
    OutcomeEvidence,
    ReasonCodeVocabulary,
    canonical_message,
)

FENCE = str(uuid4())
DIAGNOSTICS = {
    "schema_version": 1,
    "collected_at": "2026-10-05T00:00:00+00:00",
    "phase": "start",
    "category": "runtime",
    "stdout": {
        "text": "ready",
        "truncated": False,
        "dropped_bytes": None,
        "dropped_lines": None,
    },
    "stderr": {
        "text": "",
        "truncated": False,
        "dropped_bytes": None,
        "dropped_lines": None,
    },
    "versions": [],
    "sandbox": [],
    "storage": [],
    "preflight": [],
    "collector_errors": [],
}
EVIDENCE = {
    "diagnostics": DIAGNOSTICS,
    "helper_error_code": "operation_io",
    "helper_exit_code": 2,
    "stage": "start",
    "diagnostic": "helper refused the start",
}
UNKNOWN = {
    "kind": "unknown",
    "wait_reason": "stop-unconfirmed",
    "reason": "workload stop remains unconfirmed",
    "retry_after_seconds": 7,
    "evidence": EVIDENCE,
}
FAILED = {
    "kind": "failed",
    "code": "recipe_start_failed",
    "reason": "container runtime could not start the workload",
    "failure_kind": "temporary-dependency",
    "evidence": EVIDENCE,
}
CANCELLED = {
    "kind": "failed",
    "code": "operation_cancelled",
    "reason": "controller cancellation confirmed after exact workload stop",
}
DONE = {"kind": "done", "result": {"installed_bytes": 3}}
RESULTS = [
    ("succeeded", DONE),
    ("failed", FAILED),
    ("cancelled", CANCELLED),
    ("observing", UNKNOWN),
]


def _probe() -> str:
    probe = os.environ.get("VONK_OUTCOME_WIRE_PROBE")
    if not probe:
        pytest.skip("set VONK_OUTCOME_WIRE_PROBE to the compiled probe")
    return probe


def _through_rust(kind: str, document: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [_probe(), kind],
        input=canonical_message(document),
        capture_output=True,
        check=False,
        timeout=30,
    )


def _round_trip(kind: str, document: Any) -> dict[str, Any]:
    completed = _through_rust(kind, document)
    assert completed.returncode == 0, completed.stderr.decode()
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("state", "outcome"), RESULTS, ids=lambda value: str(value)[:12]
)
def test_rust_reads_and_returns_every_outcome_arm_of_a_result(
    state: str, outcome: dict[str, Any]
) -> None:
    message = AgentResult.parse({"fence": FENCE, "state": state, "result": outcome})
    sent = json.loads(canonical_message(message))

    returned = _round_trip("agent-result", sent)

    consumed = AgentResult.model_validate_json(json.dumps(returned))
    assert consumed == message
    assert canonical_message(consumed) == canonical_message(message)


@pytest.mark.parametrize(
    ("_state", "outcome"), RESULTS, ids=lambda value: str(value)[:12]
)
def test_rust_round_trips_the_bare_outcome_envelope(
    _state: str, outcome: dict[str, Any]
) -> None:
    document = {"outcome": outcome}
    sent = json.loads(canonical_message(OutcomeCatalog.model_validate(document)))

    assert _round_trip("outcome", sent) == sent


def test_rust_round_trips_typed_evidence() -> None:
    sent = json.loads(canonical_message(OutcomeEvidence.model_validate(EVIDENCE)))

    assert _round_trip("evidence", sent) == sent


@pytest.mark.parametrize(
    "error",
    [
        {"category": "security-refusal", "reason": "grant_invalid"},
        {"category": "security-refusal", "reason": "401"},
        {"category": "invalid-request", "reason": "malformed", "field": "timeout"},
        {"category": "unknown", "reason": "lease-lapsed"},
    ],
    ids=lambda value: value["category"] + ":" + value["reason"],
)
def test_rust_round_trips_every_error_category(error: dict[str, Any]) -> None:
    sent = json.loads(canonical_message(ErrorCatalog.model_validate({"error": error})))

    assert _round_trip("error", sent) == sent


def _vocabulary_documents(
    carrier: type[BaseModel] = LifecycleVocabulary,
) -> list[dict[str, str]]:
    members: dict[str, list[str]] = {}
    for name, field in carrier.model_fields.items():
        enum = field.annotation
        assert isinstance(enum, type) and issubclass(enum, Enum)
        members[name] = [member.value for member in enum]
    longest = max(len(values) for values in members.values())
    return [
        {name: values[index % len(values)] for name, values in members.items()}
        for index in range(longest)
    ]


def test_rust_knows_every_word_of_the_vocabulary() -> None:
    documents = _vocabulary_documents()
    assert documents
    for document in documents:
        sent = json.loads(
            canonical_message(LifecycleVocabulary.model_validate(document))
        )
        assert _round_trip("vocabulary", sent) == sent


def test_rust_knows_every_reason_code() -> None:
    documents = _vocabulary_documents(ReasonCodeVocabulary)
    assert documents
    for document in documents:
        sent = json.loads(
            canonical_message(ReasonCodeVocabulary.model_validate(document))
        )
        assert _round_trip("reason-codes", sent) == sent


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        ("vocabulary", {"state": "waiting-for-operator"}),
        ("reason-codes", {"model_cache_code": "model_cache.made_up"}),
        ("agent-result", {"fence": FENCE, "state": "failed", "result": UNKNOWN}),
        (
            "agent-result",
            {
                "fence": FENCE,
                "state": "failed",
                "result": {**FAILED, "code": "made_up_code"},
            },
        ),
        (
            "agent-result",
            {
                "fence": FENCE,
                "state": "waiting-for-operator",
                "result": {**UNKNOWN, "wait_reason": "operator-should-look"},
            },
        ),
        (
            "agent-result",
            {
                "fence": FENCE,
                "state": "waiting-for-operator",
                "result": {**UNKNOWN, "kind": "failed"},
            },
        ),
        ("error", {"error": {"category": "unknown", "reason": "malformed"}}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_rust_refuses_what_the_contract_refuses(kind: str, document: Any) -> None:
    completed = _through_rust(kind, document)

    assert completed.returncode != 0
    with pytest.raises(ValueError):
        {
            "agent-result": AgentResult.parse,
            "error": ErrorCatalog.model_validate,
            "vocabulary": LifecycleVocabulary.model_validate,
            "reason-codes": ReasonCodeVocabulary.model_validate,
        }[kind](document)
