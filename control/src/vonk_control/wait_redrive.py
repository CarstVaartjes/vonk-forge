"""Bind each existing wait owner to its normal Controller advancement path."""

from __future__ import annotations

from collections.abc import Mapping

from vonk_agent_protocol import WaitOwnerKind

from .due_waits import AdvanceWait, WaitSource
from .models import (
    AgentCertificate,
    AgentIssuedCertificateRevocation,
    AgentOperation,
    ArtifactJob,
    ArtifactLifecycleGate,
    Base,
    CoordinatorFailure,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    ModelCacheOperation,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRun,
    RoutePublicationOwner,
)

# This is dispatch coverage, not a duplicate state/reason vocabulary. The
# whole-tree guard compares both the ORM owners and concrete KindAdapters.
ADAPTER_WAIT_KINDS = {
    "AgentOperationAdapter": WaitOwnerKind.AGENT_OPERATION,
    "AgentUpgradeAdapter": WaitOwnerKind.AGENT_UPGRADE,
    "ArtifactJobAdapter": WaitOwnerKind.ARTIFACT_JOB,
    "FleetProfileAdapter": WaitOwnerKind.FLEET_PROFILE,
    "ImageAvailabilityAdapter": WaitOwnerKind.IMAGE_AVAILABILITY,
    "PrebuiltImageAdapter": WaitOwnerKind.PREBUILT_IMAGE,
    "JobAdapter": WaitOwnerKind.JOB,
    "ModelCacheAdapter": WaitOwnerKind.MODEL_CACHE,
    "RecipeOperationAdapter": WaitOwnerKind.RECIPE_OPERATION,
    "RecipeUpdateBatchAdapter": WaitOwnerKind.RECIPE_UPDATE_BATCH,
    "RunSwitchAdapter": WaitOwnerKind.RUN_SWITCH,
}

WAIT_MODELS: Mapping[WaitOwnerKind, type[Base]] = {
    WaitOwnerKind.COORDINATOR: CoordinatorFailure,
    WaitOwnerKind.AGENT_OPERATION: AgentOperation,
    WaitOwnerKind.AGENT_UPGRADE: Job,
    WaitOwnerKind.ARTIFACT_JOB: ArtifactJob,
    WaitOwnerKind.FLEET_PROFILE: FleetProfileApplication,
    WaitOwnerKind.IMAGE_AVAILABILITY: Job,
    WaitOwnerKind.PREBUILT_IMAGE: Job,
    WaitOwnerKind.JOB: Job,
    WaitOwnerKind.MODEL_CACHE: ModelCacheOperation,
    WaitOwnerKind.RECIPE_OPERATION: Job,
    WaitOwnerKind.RECIPE_UPDATE_BATCH: Job,
    WaitOwnerKind.RUN_SWITCH: Job,
    WaitOwnerKind.SELECTED_PROFILE: FleetProfileSelection,
    WaitOwnerKind.ROUTE_PUBLICATION: RoutePublicationOwner,
    WaitOwnerKind.ROUTE_RECOVERY: RecipeRun,
    WaitOwnerKind.STORAGE_RELIEF: ArtifactLifecycleGate,
    WaitOwnerKind.CLEANUP: RecipeInstallation,
    WaitOwnerKind.CATALOG_SYNC: RecipeLibrarySyncRun,
    WaitOwnerKind.CERTIFICATE: AgentCertificate,
    WaitOwnerKind.CERTIFICATE_REVOCATION: AgentIssuedCertificateRevocation,
}


def wait_sources(
    advancers: Mapping[WaitOwnerKind, AdvanceWait],
) -> tuple[WaitSource, ...]:
    if advancers.keys() != WAIT_MODELS.keys():
        raise ValueError(
            "every persisted wait kind requires its owner advancement function"
        )
    return tuple(
        WaitSource(model, frozenset({kind}), advancers[kind])
        for kind, model in WAIT_MODELS.items()
    )
