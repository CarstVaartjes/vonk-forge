"""The result evidence of an artifact job, as one typed contract.

Every writer is the Controller itself: the agent's measurements of the run
(``elapsed_milliseconds``, ``peak_memory_bytes``), the cancellation that was
requested (``cancel_*``) and the residue record of an effect left in doubt
(``failure_kind`` and the flags beside it).  No engine adds keys of its own, so
the contract is closed.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import ConfigDict, Field
from vonk_agent_protocol import canonical_message

from .lifecycle.evidence import BookkeepingReason, retire_as_unknown
from .strict_json import StrictJSONModel, read_stored_model, serialize_json_value

#: Every rule of a job's output contract that a manifest can break: the
#: limits the job was submitted with, then the recipe's output slots.
OutputContractRule = Literal[
    "job_output_allowed_media_type",
    "job_output_max_files",
    "job_output_max_file_bytes",
    "job_output_max_total_bytes",
    "job_output_slot",
    "job_output_slot_max_file_bytes",
    "job_output_slot_files",
    "job_output_slot_max_total_bytes",
    "job_output_contract_max_total_bytes",
]
OUTPUT_CONTRACT_RULES: tuple[OutputContractRule, ...] = get_args(OutputContractRule)


class OutputContractBreach(StrictJSONModel):
    """The output-contract rule a job's manifest broke, measured."""

    model_config = ConfigDict(extra="forbid", strict=True)

    rule: OutputContractRule
    #: The bound that was crossed and the measured value, for a rule with one.
    limit: int | None = Field(default=None, ge=0)
    observed: int | None = Field(default=None, ge=0)
    #: The output file that broke the rule, for a rule about one file.
    entry: str | None = Field(default=None, max_length=128)
    #: The recipe output slot, for a rule about one slot.
    slot: str | None = Field(default=None, max_length=32)


class ArtifactJobResultEvidence(StrictJSONModel):
    """What the Controller knows about how a job ended, to every field."""

    model_config = ConfigDict(extra="forbid", strict=True)

    elapsed_milliseconds: int | None = Field(default=None, ge=0)
    peak_memory_bytes: int | None = Field(default=None, ge=0)
    #: The job process's measured exit code, from the agent's receipt.
    exit_code: int | None = Field(default=None, ge=0, le=255)
    cancel_request_id: str | None = Field(default=None, max_length=128)
    cancel_actor: str | None = Field(default=None, max_length=256)
    cancel_reason: str | None = Field(default=None, max_length=512)
    failure_kind: (
        Literal["cancellation-stop-uncertain", "agent-lease-expired"] | None
    ) = None
    recoverable: bool | None = None
    active_scope_may_remain: bool | None = None
    late_results_accepted: bool | None = None
    residue_resolved_by: Literal["exact-stop"] | None = None
    #: Why the job's outputs were refused: the job ended failed at once.
    output_breach: OutputContractBreach | None = None

    @property
    def scope_unproven(self) -> bool:
        """An omitted flag cannot erase an unresolved typed physical cause."""
        if self.residue_resolved_by == "exact-stop":
            return False
        # The sole false-scope writer records its validated exact receipt marker
        # alongside the flag. A contradictory false flag alone cannot erase a
        # still-unresolved typed uncertainty cause.
        return self.failure_kind is not None or self.active_scope_may_remain is True

    def merged(self, given: ArtifactJobResultEvidence) -> ArtifactJobResultEvidence:
        """These facts with every field ``given`` set, the new value winning."""

        return self.model_copy(
            update={name: getattr(given, name) for name in given.model_fields_set}
        )


def read_result_evidence(value: object) -> ArtifactJobResultEvidence | None:
    """Read a stored evidence document; damaged evidence is no evidence.

    Nothing re-derives it, so a document that does not read is retired as
    unknown and the job is shown without it.
    """

    if value is None:
        return None
    try:
        return read_stored_model(
            ArtifactJobResultEvidence, canonical_message(value), from_json=True
        )
    except (TypeError, ValueError) as error:
        retire_as_unknown(
            "artifact-job.result-evidence",
            "stored-evidence",
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"{type(error).__name__}: {error}",
        )
        return None


def dump_result_evidence(
    evidence: ArtifactJobResultEvidence | None,
) -> dict[str, object] | None:
    """Store all current facts, including values mutated outside field tracking."""

    if evidence is None:
        return None
    document = serialize_json_value(evidence)
    return document or None


__all__ = [
    "OUTPUT_CONTRACT_RULES",
    "ArtifactJobResultEvidence",
    "OutputContractBreach",
    "OutputContractRule",
    "dump_result_evidence",
    "read_result_evidence",
]
