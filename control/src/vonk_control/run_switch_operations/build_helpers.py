"""Build helpers."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
    WaitReason,
)

from ..models import (
    CatalogDocumentRevision,
    RecipeBuild,
)
from ..run_switch_contract import (
    FreshnessEvidence,
    RunSwitchContainerBuildResult,
)
from .constants import _CONTAINER_BUILD_STATE_ADAPTER
from .errors import RunSwitchRetryLater
from .identity_helpers import _recipe_definition


def _refreshed_freshness(
    reviewed: Sequence[FreshnessEvidence], fresh: Sequence[FreshnessEvidence]
) -> list[FreshnessEvidence]:
    """The reviewed evidence, with each source the recheck read replaced by it."""

    current = {item.source: item for item in fresh}
    kept = [current.pop(item.source, item) for item in reviewed]
    return [*kept, *current.values()]


def _recipe_model_digests(revision: CatalogDocumentRevision | None) -> frozenset[str]:
    """The content digests of the models a recipe revision names."""

    recipe = _recipe_definition(revision.document) if revision is not None else None
    return (
        frozenset(item.model.content_sha256 for item in recipe.models)
        if recipe is not None
        else frozenset()
    )


def _container_build_result(build: RecipeBuild) -> RunSwitchContainerBuildResult:
    """Project the authoritative build record into its phase receipt."""
    succeeded = build.state == LifecycleState.SUCCEEDED.value
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
