"""The typed kind of a control-plane note stored as a row's ``status_reason``.

A refusal note (a claim, heartbeat or result the Controller declined) is
evidence about work that is not progressing, never an outcome, so it may be
replaced by a newer note and must never erase a domain reason.  The kind is
its own column, written with the text, so no reader decides by the text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self


class StatusNote(StrEnum):
    CLAIM_REFUSED = "claim-refused"
    CLAIM_NOTE = "claim-note"
    HEARTBEAT_REFUSED = "heartbeat-refused"
    RESULT_REFUSED = "result-refused"

    @property
    def prefix(self) -> str:
        """The rendering prefix of the note's text; display only."""

        return _PREFIX[self]


_PREFIX = {
    StatusNote.CLAIM_REFUSED: "claim refused: ",
    StatusNote.CLAIM_NOTE: "claim note: ",
    StatusNote.HEARTBEAT_REFUSED: "heartbeat refused: ",
    StatusNote.RESULT_REFUSED: "result refused: ",
}

BOUNDARY_NOTES = (StatusNote.HEARTBEAT_REFUSED, StatusNote.RESULT_REFUSED)


class NoteText(str):
    """A note's text that carries its kind into the row it is assigned to."""

    note: StatusNote

    def __new__(cls, note: StatusNote, text: str) -> Self:
        instance = super().__new__(cls, text)
        instance.note = note
        return instance


if TYPE_CHECKING:
    from .models import AgentOperation, Job

    type NotedRow = Job | AgentOperation


def carry_note(source: NotedRow, target: NotedRow) -> None:
    """Let a row that shows another row's text keep that text's kind."""

    if target.status_reason == source.status_reason:
        target.status_note = source.status_note


def is_replaceable(row: NotedRow) -> bool:
    """No reason, or only a note: a newer note may take its place."""

    return row.status_reason is None or row.status_note is not None


def validated_text(row: NotedRow, new_text: str | None) -> str | None:
    """Model validator body: a note's text records its kind, other text clears it."""

    if isinstance(new_text, NoteText):
        row.status_note = new_text.note.value
    elif new_text != row.status_reason:
        row.status_note = None
    return new_text
