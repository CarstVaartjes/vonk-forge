"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vonk_agent_protocol.agent_words import ProfileChildPhase

from ..run_switch_contract import (
    ArtifactVerificationEvidence,
    RunSwitchCachedTransferResult,
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchTargetTransferEvidenceResult,
    RunSwitchTargetTransferResult,
    RunSwitchVerifyResult,
)
from .identity import DistributionIdentity


class DistributionVerification(DistributionIdentity):
    """Verification boundary for durable artifact distribution."""

    @staticmethod
    def _verification_result(
        plan: RunSwitchPlan,
        progress: RunSwitchOperationResult,
        *,
        skipped: bool = False,
        cached_nodes: Sequence[str] = (),
        cached_target_totals: Mapping[str, int] | None = None,
        verified_image_digest: str | None = None,
        verified_oci_layout_sha256: str | None = None,
        evidence: Sequence[ArtifactVerificationEvidence] = (),
    ) -> RunSwitchVerifyResult:
        resolved_image_digest, resolved_layout_digest, _image_bytes, build_id = (
            DistributionIdentity._runtime_identity(plan, progress)
        )
        result = RunSwitchVerifyResult(
            phase=ProfileChildPhase.VERIFY.value,
            subphase=ProfileChildPhase.TARGET_COPY.value,
            skipped=skipped,
            verified=True,
            verified_digests=list(plan.storage.artifact_digests),
            verified_build_id=build_id,
            verified_image_digest=(
                verified_image_digest
                if verified_image_digest is not None
                else resolved_image_digest
            ),
            verified_oci_layout_sha256=(
                verified_oci_layout_sha256
                if verified_oci_layout_sha256 is not None
                else resolved_layout_digest
            ),
            cached_nodes=list(cached_nodes),
            cached_target_totals=dict(cached_target_totals or {}),
            evidence=list(evidence),
        )
        return result

    def _verify_evidence(
        self,
        plan: RunSwitchPlan,
        progress: RunSwitchOperationResult,
        targets: tuple[str, ...],
        cached: tuple[str, ...],
    ) -> RunSwitchVerifyResult:
        """Require a terminal agent result from every non-cached target."""
        expected_image, expected_layout, _bytes, _build_id = self._runtime_identity(
            plan, progress
        )
        receipts: dict[str, ArtifactVerificationEvidence] = {}
        cached_nodes = set(cached)
        for receipt in progress.phase_results:
            if isinstance(receipt, RunSwitchTargetTransferResult):
                for node_id, assignment in receipt.assignments.items():
                    if (
                        node_id != assignment.node_id
                        or assignment.plan_digest != plan.plan_digest
                        or assignment.oci_image_digest != expected_image
                        or assignment.oci_archive_sha256 != expected_layout
                        or (
                            plan.preparation is not None
                            and assignment.model_artifact_set_sha256
                            != plan.preparation.model.artifact_set_sha256
                        )
                    ):
                        raise RuntimeError(
                            "retained distribution assignment differs from the exact plan"
                        )
                cached_nodes.update(receipt.cached_nodes)
            elif isinstance(
                receipt, (RunSwitchCachedTransferResult, RunSwitchVerifyResult)
            ):
                if (
                    receipt.verified_image_digest != expected_image
                    or receipt.verified_oci_layout_sha256 != expected_layout
                ):
                    raise RuntimeError(
                        "retained verification identity differs from the exact plan"
                    )
                cached_nodes.update(receipt.cached_nodes)
            if isinstance(receipt, RunSwitchVerifyResult):
                for evidence in receipt.evidence:
                    if evidence.node_id in targets:
                        receipts[evidence.node_id] = evidence
            elif (
                isinstance(receipt, RunSwitchTargetTransferEvidenceResult)
                and receipt.node_id in targets
            ):
                receipts[receipt.node_id] = receipt
        missing = set(targets) - cached_nodes
        if missing - receipts.keys():
            raise RuntimeError(
                "verification requires terminal evidence from every target"
            )
        if any(
            receipts[node_id].uncertain
            or receipts[node_id].error is not None
            or receipts[node_id].failure_kind is not None
            or receipts[node_id].error_code is not None
            for node_id in missing
        ):
            raise RuntimeError(
                "verification requires successful terminal target evidence"
            )
        return self._verification_result(
            plan,
            progress,
            cached_nodes=sorted(cached_nodes),
            verified_image_digest=expected_image,
            verified_oci_layout_sha256=expected_layout,
            evidence=[receipts[node_id] for node_id in sorted(receipts)],
        )
