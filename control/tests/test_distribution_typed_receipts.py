"""Exact distribution identity and complete retained receipts survive restart."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from vonk_agent_protocol import canonical_message
from vonk_control.distribution_executor import (
    DurableDistributionPhaseExecutor,
    _phase_receipt,
)
from vonk_control.run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchRuntimeImageResult,
    RunSwitchTargetTransferEvidenceResult,
)
from vonk_control.runtime_image_preparation import RuntimeImageReceipt

NODE = "spk_" + "1" * 32
IMAGE = "sha256:" + "a" * 64
LAYOUT = "b" * 64


def _plan() -> RunSwitchPlan:
    return RunSwitchPlan.model_construct(
        image_digest=IMAGE,
        recipe_build_id=str(uuid4()),
        preparation=None,
        build=SimpleNamespace(
            oci_layout_sha256=LAYOUT, image_bytes=11, build_input_sha256=None
        ),
        storage=SimpleNamespace(artifact_digests=["c" * 64]),
    )


def test_terminal_handoff_retains_diagnostic_and_verifies_after_restart() -> None:
    receipt = _phase_receipt(
        {
            "node_id": NODE,
            "downloaded_bytes": 7,
            "diagnostic": "exact transfer completed after resumed checkpoint",
        },
        phase=RunSwitchPhase.model_construct(kind="transfer", subphase="target-copy"),
    )
    assert isinstance(receipt, RunSwitchTargetTransferEvidenceResult)
    stored = RunSwitchOperationResult(phase_results=[receipt]).model_dump(mode="json")
    restored = RunSwitchOperationResult.model_validate_json(canonical_message(stored))
    executor = object.__new__(DurableDistributionPhaseExecutor)
    result = executor._verify_evidence(_plan(), restored, (NODE,), ())
    assert result.verified is True
    assert result.model_dump(mode="json")["evidence"] == [
        {
            "node_id": NODE,
            "downloaded_bytes": 7,
            "copied_bytes": None,
            "error": None,
            "reason": None,
            "uncertain": False,
            "failure_kind": None,
            "error_code": None,
            "diagnostic": receipt.diagnostic,
        }
    ]


def test_missing_terminal_handoff_never_implies_success() -> None:
    executor = object.__new__(DurableDistributionPhaseExecutor)
    with pytest.raises(RuntimeError, match="terminal evidence from every target"):
        executor._verify_evidence(_plan(), RunSwitchOperationResult(), (NODE,), ())
    failed = RunSwitchTargetTransferEvidenceResult(
        phase="transfer",
        subphase="target-copy",
        node_id=NODE,
        downloaded_bytes=7,
        error="digest mismatch",
        failure_kind="integrity",
    )
    with pytest.raises(RuntimeError, match="successful terminal target evidence"):
        executor._verify_evidence(
            _plan(), RunSwitchOperationResult(phase_results=[failed]), (NODE,), ()
        )


def test_runtime_identity_refuses_retained_image_drift() -> None:
    runtime = RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher="vonk",
        distribution_slug="fixture",
        distribution_content_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
        oci_archive_sha256=LAYOUT,
        image_bytes=11,
        local_image_config_id="sha256:" + "e" * 64,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        archive_path="/managed/fixture",
        recorded_at="2026-10-06T12:00:00Z",
        build_id=str(uuid4()),
        runtime_interface_label="v1",
        runtime_adapter="fixture",
        runtime_adapter_sha256="f" * 64,
    )
    receipt = RunSwitchRuntimeImageResult(
        phase="prepare",
        subphase="runtime-image",
        runtime_image=runtime,
        image_digest=runtime.image_digest,
        oci_layout_sha256=runtime.oci_archive_sha256,
        image_bytes=runtime.image_bytes,
        build_id=runtime.build_id,
    )
    with pytest.raises(RuntimeError, match="differs from the exact build"):
        DurableDistributionPhaseExecutor._runtime_identity(
            _plan(), RunSwitchOperationResult(phase_results=[receipt])
        )
