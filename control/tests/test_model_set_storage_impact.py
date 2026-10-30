"""A model is one set (digest plus bytes); its file count never reaches the plan."""

from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchCachedTransferResult,
)
from vonk_control.run_switch_operations.interfaces import ArtifactInspection
from vonk_control.run_switch_operations.phase_planning import PhasePlanningMixin

FILES = 300  # one production model had 286 files and failed planning at 256
SET_DIGEST = "a" * 64


def _inspection() -> ArtifactInspection:
    return ArtifactInspection(
        required_bytes=FILES,
        reused_bytes=0,
        copied_bytes=0,
        missing_nas_bytes=0,
        missing_spark_bytes=0,
        reclaimable_bytes=0,
        nas_coverage="complete",
        spark_coverage="complete",
        artifact_set_sha256=SET_DIGEST,
        artifact_set_bytes=FILES,
        artifact_count=FILES,
    )


def test_a_model_of_hundreds_of_files_plans_as_one_set() -> None:
    impact = PhasePlanningMixin._storage(_inspection(), retention="retain-cached")
    assert impact.artifact_set_sha256 == SET_DIGEST
    assert impact.artifact_set_bytes == FILES
    assert ArtifactStorageImpact.model_validate(impact.model_dump(mode="json"))


def test_the_plan_contract_lists_no_files() -> None:
    assert "artifact_digests" not in ArtifactStorageImpact.model_fields
    assert "verified_digests" not in RunSwitchCachedTransferResult.model_fields
    assert "artifact_digests" not in ArtifactInspection.__dataclass_fields__
