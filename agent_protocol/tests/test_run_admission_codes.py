"""The run admission and resource blocker codes are closed, typed wire words."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    LifecycleVocabulary,
    ResourceBlockerCode,
    RunAdmissionCode,
)


@pytest.mark.parametrize("enum", [RunAdmissionCode, ResourceBlockerCode])
def test_every_member_round_trips_through_the_wire_model(enum) -> None:
    for member in enum:
        assert enum(member.value) is member
        assert LifecycleVocabulary.model_validate(
            {**_sample(), _field(enum): member.value}
        )


@pytest.mark.parametrize(
    ("enum", "word"),
    [
        (RunAdmissionCode, "run.not_a_code"),
        (ResourceBlockerCode, "resource.not_a_code"),
        (RunAdmissionCode, "resource.capacity_unknown"),
    ],
)
def test_the_vocabulary_is_closed(enum, word: str) -> None:
    with pytest.raises(ValueError):
        enum(word)
    with pytest.raises(ValueError):
        LifecycleVocabulary.model_validate({**_sample(), _field(enum): word})


def test_the_words_keep_their_namespaces() -> None:
    assert all(code.value.startswith("run.") for code in RunAdmissionCode)
    assert all(code.value.startswith("resource.") for code in ResourceBlockerCode)
    assert RunAdmissionCode.CAPACITY_BUSY == "run.capacity_busy"
    assert (
        ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN == "resource.resident_usage_unknown"
    )


def _field(enum) -> str:
    return next(
        name
        for name, field in LifecycleVocabulary.model_fields.items()
        if field.annotation is enum
    )


def _sample() -> dict[str, str]:
    return {
        name: next(iter(field.annotation)).value
        for name, field in LifecycleVocabulary.model_fields.items()
    }
