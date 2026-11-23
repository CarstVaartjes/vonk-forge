"""Build helpers."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from vonk_agent_protocol import (
    RunSwitchCode,
    WaitReason,
)

from ..models.cache import RecipeBuild
from ..run_switch_contract.evidence import FreshnessEvidence
from ..run_switch_contract.phase_results import RunSwitchContainerBuildResult
from ..stored.states import RecipeBuildState
from .constants import _CONTAINER_BUILD_STATE_ADAPTER
from .errors import RunSwitchRetryLater


def _refreshed_freshness(
    reviewed: Sequence[FreshnessEvidence], fresh: Sequence[FreshnessEvidence]
) -> list[FreshnessEvidence]:
    """The reviewed evidence, with each source the recheck read replaced by it."""

    current = {item.source: item for item in fresh}
    kept = [current.get(item.source, item) for item in reviewed]
    reviewed_sources = {item.source for item in reviewed}
    return [*kept, *(item for item in fresh if item.source not in reviewed_sources)]


def _container_build_result(build: RecipeBuild) -> RunSwitchContainerBuildResult:
    """Project the authoritative build record into its phase receipt."""
    succeeded = build.state == RecipeBuildState.SUCCEEDED.value
    try:
        receipt = RunSwitchContainerBuildResult(
            phase="prepare",
            subphase="container-build",
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            state=_CONTAINER_BUILD_STATE_ADAPTER.validate_python(
                build.state, strict=True
            ),
            image_digest=build.image_digest if succeeded else None,
            oci_layout_sha256=build.oci_layout_sha256 if succeeded else None,
            image_bytes=build.image_bytes if succeeded else None,
        )
    except ValidationError as error:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_EVIDENCE_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    return receipt
