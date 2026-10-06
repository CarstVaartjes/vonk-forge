"""The reason codes are one definition in every generated surface.

Each closed domain enum of ``vonk_agent_protocol.reason_codes`` must read the same
in the Pydantic contract, the exported wire JSON Schema, the generated Rust
declarations, the Controller's OpenAPI document, the generated TypeScript types
and the runtime constants the web app imports.  The generators have their own
staleness tests; this one proves the *content* agrees, so a hand edit to any
generated file, or a contract change that was not regenerated, fails here with
the surface named.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    REASON_CODE_ENUMS,
    RETIRED_CODE_SPELLINGS,
    FailureCode,
    InvalidRequestReason,
    ReasonCodeVocabulary,
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    RunSwitchCode,
    SecurityRefusalReason,
    WaitReason,
    adopt_reason_code,
    reason_code_of,
    resource_term_code,
    run_switch_code,
)

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json"
GENERATED_RUST = ROOT / "rust/crates/vonk-agent-protocol/src/generated.rs"
OPENAPI = ROOT / "control/openapi.json"
TYPESCRIPT_CONSTANTS = ROOT / "control/web/src/api/vocabulary.generated.ts"


def _words(enum: type) -> list[str]:
    return [member.value for member in enum]  # type: ignore[attr-defined]


def _rust_enum(source: str, name: str) -> list[str]:
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


def _typescript_constants(source: str, name: str) -> list[str]:
    match = re.search(
        rf"export const {name} = \{{(.*?)\n\}} as const;", source, re.DOTALL
    )
    assert match, f"{name} is not exported by vocabulary.generated.ts"
    return re.findall(r': "([^"]*)",', match.group(1))


@pytest.mark.parametrize("enum", REASON_CODE_ENUMS, ids=lambda enum: enum.__name__)
def test_every_surface_agrees_with_the_contract(enum: type) -> None:
    words = _words(enum)
    name = enum.__name__

    assert json.loads(WIRE.read_text())["$defs"][name]["enum"] == words, "wire.json"
    assert sorted(_rust_enum(GENERATED_RUST.read_text(), name)) == sorted(words), (
        "generated.rs"
    )
    assert json.loads(OPENAPI.read_text())["components"]["schemas"][name]["enum"] == (
        words
    ), "control/openapi.json"
    assert _typescript_constants(TYPESCRIPT_CONSTANTS.read_text(), name) == words, (
        "vocabulary.generated.ts"
    )


def test_the_carrier_publishes_every_enum_once() -> None:
    published = [
        field.annotation for field in ReasonCodeVocabulary.model_fields.values()
    ]

    assert sorted(getattr(enum, "__name__", "") for enum in published) == sorted(
        enum.__name__ for enum in REASON_CODE_ENUMS
    )
    assert len(set(published)) == len(published)


@pytest.mark.parametrize("enum", REASON_CODE_ENUMS, ids=lambda enum: enum.__name__)
def test_a_domain_spells_each_word_once(enum: type) -> None:
    words = _words(enum)

    assert len(words) == len(set(words))
    # The Rust declarations cannot tell ``a-b`` from ``a_b``.
    assert len({re.sub(r"[^a-z0-9]", "", word.lower()) for word in words}) == len(words)


def test_a_dotted_word_belongs_to_one_domain_and_to_no_older_vocabulary() -> None:
    older = {
        member.value
        for enum in (
            SecurityRefusalReason,
            WaitReason,
            InvalidRequestReason,
            FailureCode,
        )
        for member in enum
    }
    owners: dict[str, str] = {}
    # An installation and a run share five degraded-group words by design.
    shared = {"InstallDegradedReason", "RunDegradedReason"}
    for enum in REASON_CODE_ENUMS:
        if enum.__name__ in shared:
            continue
        for word in _words(enum):
            if "." not in word and "-" not in word:
                continue
            assert word not in older, f"{word} is already an older contract word"
            assert owners.setdefault(word, enum.__name__) == enum.__name__, word


def test_a_retired_spelling_reads_as_the_current_one() -> None:
    for retired, current in RETIRED_CODE_SPELLINGS.items():
        assert retired not in _words(RunSwitchCode)
        assert adopt_reason_code(retired) == current.value
        assert reason_code_of(retired) is current
    assert adopt_reason_code("run-switch.waiting") == "run-switch.waiting"
    assert reason_code_of("a.code.nobody.owns") is None


def test_the_wrappers_are_total_over_their_closed_inputs() -> None:
    for term in ResourceTerm:
        for problem in ResourceTermProblem:
            assert resource_term_code(term, problem) in set(ResourcePlanningCode)
    for inner in ("resource.insufficient", "reconcile.operation_active"):
        assert run_switch_code(inner).value == f"run-switch.{inner}"
    assert run_switch_code("something.new") is RunSwitchCode.REASON_UNCLASSIFIED
