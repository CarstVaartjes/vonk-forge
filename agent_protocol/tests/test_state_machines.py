"""The state machines of the stored records are closed contract vocabularies."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    MACHINE_ALIASES,
    MACHINES,
    DistributionAssignmentState,
    InstallationState,
    LifecycleVocabulary,
    RouteState,
    adopt_machine_state,
    machine_adopter,
    machine_check,
    machine_words,
)


def test_every_machine_is_published_through_the_vocabulary_carrier() -> None:
    carried = {field.annotation for field in LifecycleVocabulary.model_fields.values()}
    assert set(MACHINES.values()) <= carried


def test_the_words_of_each_machine_are_distinct_and_non_empty() -> None:
    for machine in MACHINES.values():
        words = [member.value for member in machine]
        assert words
        assert len(words) == len(set(words))


def test_a_stored_word_is_adopted_by_its_normalized_spelling() -> None:
    assert adopt_machine_state(InstallationState, "installed") is (
        InstallationState.INSTALLED
    )
    assert adopt_machine_state(InstallationState, " Installed ") is (
        InstallationState.INSTALLED
    )
    assert adopt_machine_state(InstallationState, InstallationState.FAILED) is (
        InstallationState.FAILED
    )
    assert adopt_machine_state(InstallationState, "unheard-of") is None
    assert adopt_machine_state(InstallationState, 7) is None


def test_an_alias_row_maps_to_a_member_of_its_own_machine() -> None:
    for machine, aliases in MACHINE_ALIASES.items():
        for retired, member in aliases.items():
            assert isinstance(member, machine)
            assert adopt_machine_state(machine, retired) is member


def test_the_adopter_passes_a_foreign_word_through_for_the_field_to_judge() -> None:
    adopt = machine_adopter(RouteState)
    assert adopt("published") is RouteState.PUBLISHED
    assert adopt("???") == "???"


def test_a_check_expression_lists_the_words_in_member_order() -> None:
    assert machine_check(DistributionAssignmentState) == (
        "state IN ('active','revoked','expired')"
    )
    assert machine_check(RouteState, "route_state") == (
        "route_state IN ('withdrawn','pending','published','failed')"
    )
    assert machine_words(RouteState, [RouteState.FAILED]) == ("failed",)


@pytest.mark.parametrize("machine", sorted(MACHINES.values(), key=lambda m: m.__name__))
def test_the_check_of_a_machine_admits_exactly_its_members(machine: type) -> None:
    expression = machine_check(machine)
    for member in machine:
        assert f"'{member.value}'" in expression
