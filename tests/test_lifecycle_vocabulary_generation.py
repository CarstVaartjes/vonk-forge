"""The lifecycle vocabulary is one definition in every generated surface.

Each closed word set of ``vonk_agent_protocol.lifecycle_vocabulary`` must read the
same in the Pydantic contract, the exported wire JSON Schema, the generated Rust
declarations, the Controller's OpenAPI document and the generated TypeScript
types.  The generators have their own staleness tests; this one proves the
*content* agrees, so a hand edit to any generated file, or a contract change that
was not regenerated, fails here with the surface named.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentResultState,
    BlockerCategory,
    ErrorCategory,
    FailureCode,
    FailureStage,
    HostHelperResponseStatus,
    InvalidRequestReason,
    LifecycleEffect,
    LifecycleEventKind,
    LifecycleState,
    LifecycleSubject,
    MigrationStep,
    OperatorActionName,
    OperatorSurface,
    OutcomeKind,
    ProgressPhase,
    ResourceBlockerCode,
    RunAdmissionCode,
    SecurityRefusalReason,
    StateAlias,
    StateWriteKind,
    StopOutcome,
    WaitReason,
    WaitVerdict,
)

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json"
GENERATED_RUST = ROOT / "rust/crates/vonk-agent-protocol/src/generated.rs"
OPENAPI = ROOT / "control/openapi.json"
GENERATED_TYPESCRIPT = ROOT / "control/web/src/api/generated.d.ts"

VOCABULARY: tuple[type[Enum], ...] = (
    LifecycleState,
    AgentResultState,
    LifecycleEffect,
    OutcomeKind,
    StopOutcome,
    LifecycleEventKind,
    OperatorActionName,
    OperatorSurface,
    BlockerCategory,
    WaitVerdict,
    LifecycleSubject,
    StateAlias,
    StateWriteKind,
    MigrationStep,
    ErrorCategory,
    WaitReason,
    InvalidRequestReason,
    SecurityRefusalReason,
    FailureCode,
    RunAdmissionCode,
    ResourceBlockerCode,
    ProgressPhase,
    FailureStage,
    HostHelperResponseStatus,
)


def _words(enum: type[Enum]) -> list[str]:
    return [member.value for member in enum]


def _rust_enum(source: str, name: str) -> list[str]:
    """The wire words of a generated unit enum, in declaration order."""

    match = re.search(rf"pub enum {name} \{{(.*?)\n\}}", source, re.DOTALL)
    assert match, f"{name} is not declared in generated.rs"
    words: list[str] = []
    renamed: str | None = None
    for line in match.group(1).splitlines():
        line = line.strip()
        if rename := re.fullmatch(r'#\[serde\(rename = "([^"]+)"\)\]', line):
            renamed = rename.group(1)
        elif variant := re.fullmatch(r"([A-Za-z0-9_]+),", line):
            words.append(renamed or variant.group(1))
            renamed = None
    return words


def _typescript_union(source: str, name: str) -> list[str]:
    match = re.search(rf"^\s+{name}: ((?:\"[^\"]*\"(?: \| )?)+);", source, re.MULTILINE)
    assert match, f"{name} is not declared in the generated TypeScript"
    return re.findall(r'"([^"]*)"', match.group(1))


@pytest.mark.parametrize("enum", VOCABULARY, ids=lambda enum: enum.__name__)
def test_every_surface_agrees_with_the_contract(enum: type[Enum]) -> None:
    words = _words(enum)
    name = enum.__name__

    assert json.loads(WIRE.read_text())["$defs"][name]["enum"] == words, "wire.json"
    assert _rust_enum(GENERATED_RUST.read_text(), name) == words, "generated.rs"
    assert json.loads(OPENAPI.read_text())["components"]["schemas"][name]["enum"] == (
        words
    ), "control/openapi.json"
    assert _typescript_union(GENERATED_TYPESCRIPT.read_text(), name) == words, (
        "generated.d.ts"
    )


def test_the_unions_are_published_with_their_tags() -> None:
    wire = json.loads(WIRE.read_text())["$defs"]
    assert wire["OutcomeCatalog"]["properties"]["outcome"]["discriminator"][
        "mapping"
    ] == {
        "done": "#/$defs/OutcomeDone",
        "failed": "#/$defs/OutcomeFailed",
        "unknown": "#/$defs/OutcomeUnknown",
    }
    assert wire["ErrorCatalog"]["properties"]["error"]["discriminator"]["mapping"] == {
        "invalid-request": "#/$defs/InvalidRequest",
        "security-refusal": "#/$defs/SecurityRefusal",
        "unknown": "#/$defs/UnknownError",
    }
    # The agent result carries the typed arms beside the legacy bodies.
    result = wire["AgentResult"]["properties"]["result"]
    arms = {
        entry["$ref"].rsplit("/", 1)[-1]
        for entry in result.get("oneOf", result.get("anyOf", []))
    }
    assert {"OutcomeDone", "OutcomeFailed", "OutcomeUnknown"} <= arms
    rust = GENERATED_RUST.read_text()
    for arm, tag in (
        ("OutcomeDone", "done"),
        ("OutcomeFailed", "failed"),
        ("OutcomeUnknown", "unknown"),
    ):
        assert _rust_enum(rust, f"{arm}Kind") == [tag]
    typescript = GENERATED_TYPESCRIPT.read_text()
    assert "ErrorCatalog: {" in typescript
