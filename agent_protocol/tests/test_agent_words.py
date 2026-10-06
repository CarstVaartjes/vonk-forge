"""The agent's progress phase, failure stage and helper status are closed contract words."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    RETIRED_PROGRESS_PHASE_SPELLINGS,
    TRANSFER_PHASES,
    WAITING_PHASES,
    FailureStage,
    HostHelperResponseStatus,
    LifecycleVocabulary,
    OperationProgress,
    ProgressPhase,
    adopt_progress_phase,
)
from vonk_agent_protocol.helper_response import HostHelperResponse

ROOT = Path(__file__).resolve().parents[2]
GENERATED_RUST = ROOT / "rust/crates/vonk-agent-protocol/src/generated.rs"


def test_the_three_word_sets_are_published_through_the_vocabulary_carrier() -> None:
    carried = {field.annotation for field in LifecycleVocabulary.model_fields.values()}

    assert {ProgressPhase, FailureStage, HostHelperResponseStatus} <= carried


def test_a_retired_phase_spelling_reads_as_the_member_that_replaced_it() -> None:
    # Wrong implementation: the Controller read only current words, so an agent that
    # still reported ``download`` was shown as an unknown phase with no rate.
    for word, member in RETIRED_PROGRESS_PHASE_SPELLINGS.items():
        assert word not in {phase.value for phase in ProgressPhase}
        assert adopt_progress_phase(word) is member


def test_a_current_phase_adopts_itself_and_a_word_nobody_spells_reads_as_unknown() -> (
    None
):
    for member in ProgressPhase:
        assert adopt_progress_phase(member.value) is member
    assert adopt_progress_phase("rebuilding-the-moon") is None


def test_the_transfer_and_waiting_phases_are_members_that_do_not_overlap() -> None:
    assert TRANSFER_PHASES <= set(ProgressPhase)
    assert WAITING_PHASES <= set(ProgressPhase)
    assert not TRANSFER_PHASES & WAITING_PHASES
    # The words the Controller used to list by hand as bytes-moving all still read so.
    for word in (
        "download",
        "downloading",
        "transfer",
        "transferring",
        "copying",
        "upload",
        "uploading",
        "distribution",
    ):
        assert adopt_progress_phase(word) in TRANSFER_PHASES


def test_the_phase_stays_free_text_on_the_wire() -> None:
    # An older or newer agent's word still validates; only the writers are closed.
    for word in ("download", "a-newer-phase", ProgressPhase.BUILDING.value):
        assert OperationProgress(phase=word).phase == word


def test_each_helper_status_reads_and_a_new_one_is_refused() -> None:
    document = {"schema_version": 1, "request_id": None}
    for status in HostHelperResponseStatus:
        assert HostHelperResponse.model_validate({**document, "status": status.value})
    with pytest.raises(ValidationError):
        HostHelperResponse.model_validate({**document, "status": "maybe"})


def test_the_words_of_each_set_are_distinct_after_separators_are_dropped() -> None:
    # The Rust declarations cannot tell ``a-b`` from ``a_b``.
    for enum in (ProgressPhase, FailureStage, HostHelperResponseStatus):
        words = [member.value for member in enum]
        assert len({re.sub(r"[^a-z0-9]", "", word) for word in words}) == len(words)


def test_the_generated_rust_declares_every_member_in_order() -> None:
    source = GENERATED_RUST.read_text()
    for enum in (ProgressPhase, FailureStage, HostHelperResponseStatus):
        match = re.search(rf"pub enum {enum.__name__} \{{(.*?)\n\}}", source, re.DOTALL)
        assert match, enum.__name__
        assert re.findall(r'serde\(rename = "([^"]+)"\)', match.group(1)) == [
            member.value for member in enum
        ]
    wire = json.loads(
        (ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json").read_text()
    )["$defs"]
    assert wire["HostHelperResponse"]["properties"]["status"] == {
        "$ref": "#/$defs/HostHelperResponseStatus"
    }
