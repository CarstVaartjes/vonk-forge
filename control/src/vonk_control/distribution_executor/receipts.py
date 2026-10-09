"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter
from vonk_agent_protocol.agent_words import ProfileChildPhase

from ..run_switch_contract import (
    ArtifactVerificationEvidence,
    RunSwitchChildProgress,
    RunSwitchDistributionChildResult,
    RunSwitchPhase,
    RunSwitchPhaseResult,
)
from ..run_switch_observation_contract import RunSwitchObservedEvidence
from ..strict_json import read_stored_model


@dataclass(frozen=True, slots=True)
class RuntimeImagePull:
    """The runtime image a node pulls from the layered store."""

    image_digest: str
    config_digest: str
    address: str


@dataclass(frozen=True, slots=True)
class _ChildView:
    """Small child projection consumed by RunSwitchOperationService."""

    state: str
    result: RunSwitchDistributionChildResult | RunSwitchPhaseResult

    @property
    def progress(self) -> RunSwitchChildProgress | None:
        return getattr(self.result, "progress", None)


_PHASE_RECEIPT_ADAPTER = TypeAdapter(RunSwitchPhaseResult)


def _phase_receipt(
    value: RunSwitchPhaseResult,
    *,
    phase: RunSwitchPhase | None = None,
) -> RunSwitchPhaseResult:
    """Validate the canonical phase receipt and its accepted checkpoint."""
    try:
        receipt = _PHASE_RECEIPT_ADAPTER.validate_python(value, strict=True)
        if phase is not None:
            subphase = phase.subphase
            if subphase is None and phase.kind in {
                ProfileChildPhase.TRANSFER,
                ProfileChildPhase.VERIFY,
            }:
                subphase = ProfileChildPhase.TARGET_COPY
            if receipt.phase != phase.kind or receipt.subphase != subphase:
                raise ValueError("phase receipt belongs to a different checkpoint")
        return receipt
    except (TypeError, ValueError) as error:
        raise RuntimeError("run-switch phase receipt is invalid") from error


def _child_receipt(
    value: RunSwitchDistributionChildResult,
) -> RunSwitchDistributionChildResult:
    """The worker persists the same typed projection a reader consumes."""
    return read_stored_model(RunSwitchDistributionChildResult, value, strict=True)


def _evidence_projection(node_id: str, value: object) -> ArtifactVerificationEvidence:
    """Consume the shared partial observation without inventing missing evidence."""
    try:
        observed = read_stored_model(RunSwitchObservedEvidence, value, from_json=True)
        return ArtifactVerificationEvidence(
            node_id=node_id,
            downloaded_bytes=observed.downloaded_bytes,
            copied_bytes=observed.copied_bytes,
            error=observed.error,
            reason=observed.reason,
            uncertain=observed.uncertain,
            failure_kind=observed.failure_kind,
            error_code=observed.error_code,
            diagnostic=observed.diagnostic,
        )
    except (TypeError, ValueError):
        return ArtifactVerificationEvidence(node_id=node_id, uncertain=True)
