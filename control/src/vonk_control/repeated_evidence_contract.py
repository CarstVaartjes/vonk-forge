"""Repeated observations owned by contact or the operation they describe."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator
from vonk_agent_protocol import ContainerRuntimeAction

from .strict_json import StrictModel


class RepeatedEvidenceSubject(StrEnum):
    AGENT_RESULT = "agent result"
    HOST_RUNTIME_GRANT = "host runtime {action} grant"
    PACKAGE_ACTIVATION_GRANT = "package activation grant"
    AGENT_UPGRADE_GRANT = "agent upgrade grant"
    PROFILE_LOAD = "profile load {application_id}"


def host_grant_subject(action: ContainerRuntimeAction | str) -> str:
    return RepeatedEvidenceSubject.HOST_RUNTIME_GRANT.value.format(
        action=ContainerRuntimeAction(action).value
    )


def is_contact_subject(subject: str) -> bool:
    return subject in {
        RepeatedEvidenceSubject.AGENT_RESULT,
        RepeatedEvidenceSubject.PACKAGE_ACTIVATION_GRANT,
        RepeatedEvidenceSubject.AGENT_UPGRADE_GRANT,
        *(host_grant_subject(action) for action in ContainerRuntimeAction),
    }


class RepeatedEvidence(StrictModel):
    node_id: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=128)
    # A command success is an ordered clear marker, with no failure code.
    code: str | None = Field(default=None, min_length=1, max_length=128)
    repeats: int = Field(ge=0)
    first_at: datetime
    last_at: datetime
    detail: str | None = Field(default=None, max_length=256)
    observation_sequence: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def clear_has_no_repetitions(self) -> RepeatedEvidence:
        if self.code is None and self.repeats != 0:
            raise ValueError("a successful clear cannot contain failure repetitions")
        return self
