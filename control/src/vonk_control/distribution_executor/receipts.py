"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, TypeAdapter
from vonk_agent_protocol.agent_words import ProfileChildPhase

from ..distribution_assignment import NodeDistributionAssignment
from ..run_switch_contract import (
    ArtifactVerificationEvidence,
    RunSwitchChildProgress,
    RunSwitchDistributionChildResult,
    RunSwitchPhase,
    RunSwitchPhaseResult,
)
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
    value: Mapping[str, object] | RunSwitchPhaseResult,
    *,
    phase: RunSwitchPhase | None = None,
) -> RunSwitchPhaseResult:
    """Validate the closed phase receipt before returning or persisting it."""

    try:
        if isinstance(value, BaseModel):
            receipt = _PHASE_RECEIPT_ADAPTER.validate_python(value, strict=True)
            if phase is not None:
                expected_subphase = phase.subphase
                if expected_subphase is None and phase.kind in {
                    ProfileChildPhase.TRANSFER.value,
                    ProfileChildPhase.VERIFY.value,
                }:
                    expected_subphase = ProfileChildPhase.TARGET_COPY.value
                if receipt.phase != phase.kind or receipt.subphase != expected_subphase:
                    raise ValueError("phase receipt belongs to a different phase")
            return receipt
        normalized = dict(value)
        if phase is not None:
            if "phase" in normalized and normalized["phase"] != phase.kind:
                raise ValueError("phase receipt belongs to a different phase")
            normalized.setdefault("phase", phase.kind)
            subphase = getattr(phase, "subphase", None)
            if subphase is None and phase.kind in {
                ProfileChildPhase.TRANSFER.value,
                ProfileChildPhase.VERIFY.value,
            }:
                subphase = ProfileChildPhase.TARGET_COPY.value
            if "subphase" in normalized and normalized["subphase"] != subphase:
                raise ValueError("phase receipt belongs to a different subphase")
            normalized.setdefault("subphase", subphase)
        assignments = normalized.get("assignments")
        if isinstance(assignments, Mapping):
            normalized["assignments"] = {
                node_id: NodeDistributionAssignment.parse(raw)
                if isinstance(raw, Mapping)
                else raw
                for node_id, raw in assignments.items()
            }
        receipt = _PHASE_RECEIPT_ADAPTER.validate_python(normalized, strict=True)
    except (TypeError, ValueError) as error:
        raise RuntimeError("run-switch phase receipt is invalid") from error
    return receipt


def _child_receipt(
    value: Mapping[str, object],
    *,
    phase: RunSwitchPhase | None = None,
) -> RunSwitchDistributionChildResult:
    """Validate the persisted projection for a target-copy child Job."""

    normalized = dict(value)
    if phase is not None:
        normalized.setdefault("phase", phase.kind)
        normalized.setdefault("subphase", ProfileChildPhase.TARGET_COPY.value)
    else:
        normalized.setdefault("phase", ProfileChildPhase.TRANSFER.value)
        normalized.setdefault("subphase", ProfileChildPhase.TARGET_COPY.value)
    try:
        receipt = read_stored_model(
            RunSwitchDistributionChildResult, normalized, strict=True
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("distribution child receipt is invalid") from error
    return receipt


_FAILURE_TEXT = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


def _typed_failure(value: Mapping[str, object]) -> dict[str, str]:
    """The agent's typed failure fields, bounded to the member contract."""

    typed: dict[str, str] = {}
    for key in ("failure_kind", "error_code"):
        item = value.get(key)
        if isinstance(item, str) and _FAILURE_TEXT.fullmatch(item):
            typed[key] = item
    diagnostic = value.get("diagnostic")
    if isinstance(diagnostic, str) and diagnostic:
        typed["diagnostic"] = diagnostic[:512]
    return typed


def _evidence_projection(
    node_id: str, value: Mapping[str, object]
) -> ArtifactVerificationEvidence:
    """Keep the high-level evidence fields from an agent handoff receipt."""

    return read_stored_model(
        ArtifactVerificationEvidence,
        {
            "node_id": node_id,
            **{
                key: value[key]
                for key in (
                    "downloaded_bytes",
                    "copied_bytes",
                    "error",
                    "reason",
                    "uncertain",
                )
                if key in value
            },
            **_typed_failure(value),
        },
    )
