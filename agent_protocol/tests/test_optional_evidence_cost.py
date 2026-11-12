"""Recovering from invalid optional evidence costs one validation per group."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from vonk_agent_protocol.optional_evidence import (
    EvidenceGroup,
    OptionalEvidenceModel,
    fail_open_on_optional_evidence,
)
from vonk_agent_protocol.reason_codes import AgentEvidenceCode

GROUPS = 8


class _Probe(OptionalEvidenceModel, BaseModel):
    model_config = ConfigDict(extra="forbid")
    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = tuple(
        EvidenceGroup(AgentEvidenceCode.TELEMETRY_READING_DROPPED, ((f"g{n}",),))
        for n in range(GROUPS)
    )
    validations: ClassVar[list[int]] = [0]
    core: int
    g0: int | None = Field(default=None, ge=0)
    g1: int | None = Field(default=None, ge=0)
    g2: int | None = Field(default=None, ge=0)
    g3: int | None = Field(default=None, ge=0)
    g4: int | None = Field(default=None, ge=0)
    g5: int | None = Field(default=None, ge=0)
    g6: int | None = Field(default=None, ge=0)
    g7: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def count(cls, data: Any) -> Any:
        cls.validations[0] += 1
        return data

    drop_invalid_optional_evidence = fail_open_on_optional_evidence()


def _validations(document: dict[str, Any]) -> int:
    _Probe.validations[0] = 0
    try:
        _Probe.model_validate(document)
    except ValidationError:
        pass
    return _Probe.validations[0]


def _document(invalid: int, **extra: Any) -> dict[str, Any]:
    return {"core": 1, **{f"g{n}": -1 for n in range(invalid)}, **extra}


@pytest.mark.parametrize("invalid", [2, GROUPS])
def test_invalid_groups_cost_at_most_one_validation_each_plus_two(
    invalid: int,
) -> None:
    assert _validations(_document(invalid)) <= invalid + 2
    assert len(_Probe.model_validate(_document(invalid)).evidence_warnings) == invalid


@pytest.mark.parametrize(
    "document",
    [_document(GROUPS, core="x"), _document(GROUPS, unknown=1)],
    ids=["invalid core", "unknown field"],
)
def test_a_refused_core_fails_after_one_extra_trial(document: dict[str, Any]) -> None:
    assert _validations(document) == 2
    with pytest.raises(ValidationError):
        _Probe.model_validate(document)
