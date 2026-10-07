"""Append-only evidence for one narrowly re-derived Run/Switch measurement."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from vonk_agent_protocol import OperationProgress, canonical_message

from .run_switch_identity_contract import NodeId, RunSwitchCancellation, UuidId
from .strict_json import StrictModel

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class JournalRepairCode(StrEnum):
    EXHAUSTED = "run-switch.journal-repair-exhausted"
    EVIDENCE_UNAVAILABLE = "run-switch.journal-repair-evidence-unavailable"


class JournalRepairDisposition(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    REPAIRED = "repaired"
    DEFERRED = "deferred"
    ENDED = "ended"


class JournalRepairPurpose(StrEnum):
    MEASUREMENT = "measurement"
    CANCELLATION = "cancellation"
    OWNER_OBSERVATION = "owner_observation"


class NativeProgressWitness(StrictModel):
    operation_id: UuidId
    attempt_id: UuidId
    node_id: NodeId
    certificate_serial: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    fence: UuidId
    payload_digest: Digest
    sample_digest: Digest | None = None
    sample: OperationProgress | None = None

    @model_validator(mode="after")
    def sample_is_exact(self) -> NativeProgressWitness:
        if (self.sample is None) != (self.sample_digest is None):
            raise ValueError("native sample and digest must be present together")
        if self.sample is not None and (
            hashlib.sha256(canonical_message(self.sample)).hexdigest()
            != self.sample_digest
        ):
            raise ValueError("native progress witness digest is inconsistent")
        return self


class RunSwitchJournalRepairEvidence(StrictModel):
    """Historical evidence only: no runnable state, clock, or alternate intent."""

    algorithm: Literal["zero-transfer-native-install-v1"]
    purpose: JournalRepairPurpose
    operation_id: UuidId
    request_key: UuidId
    payload_digest: Digest
    plan_digest: Digest
    original_digest: Digest
    corrected_digest: Digest
    # Preserve the SQL document's explicit canonical encoding, including nulls.
    # It may be invalid as live progress and never supplies execution authority.
    original_document: str
    native_samples: list[NativeProgressWitness] = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def original_is_exact(self) -> RunSwitchJournalRepairEvidence:
        if self.purpose == JournalRepairPurpose.MEASUREMENT and not any(
            item.sample is not None for item in self.native_samples
        ):
            raise ValueError("measurement repair requires an exact native sample")
        value = json.loads(self.original_document)
        if not isinstance(value, dict):
            raise TypeError("original journal must be a document")
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if (
            encoded != self.original_document
            or hashlib.sha256(encoded.encode()).hexdigest() != self.original_digest
        ):
            raise ValueError("original journal evidence digest is inconsistent")
        if len({sample.operation_id for sample in self.native_samples}) != len(
            self.native_samples
        ):
            raise ValueError("native sample witnesses must have unique owners")
        return self


class RunSwitchJournalRepairPendingState(StrictModel):
    """Durable observation and cancellation while Job.result remains untouched."""

    deadline_at: datetime
    next_attempt_at: datetime
    attempts: int = Field(default=0, ge=0)
    cancellation: RunSwitchCancellation | None = None


class RunSwitchJournalRepairEndEvidence(StrictModel):
    """An ended observation retains the original journal and cancel request."""

    code: Literal["run-switch.journal-repair-exhausted"]
    operation_id: UuidId
    request_key: UuidId
    original_digest: Digest
    original_document: str
    cancellation: RunSwitchCancellation | None = None
    recorded_at: datetime

    @model_validator(mode="after")
    def original_is_exact(self) -> RunSwitchJournalRepairEndEvidence:
        encoded = json.dumps(
            json.loads(self.original_document),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if (
            encoded != self.original_document
            or hashlib.sha256(encoded.encode()).hexdigest() != self.original_digest
        ):
            raise ValueError("ended journal evidence digest is inconsistent")
        return self
