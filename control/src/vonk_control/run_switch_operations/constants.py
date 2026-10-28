"""Constants."""

from __future__ import annotations

import logging

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    ResourceBlockerCode,
    RunSwitchCode,
)
from vonk_forge_contracts.recipe import Scalar

from .. import job_states
from ..models import (
    CatalogDocumentRevision,
)
from ..run_switch_contract import (
    RunSwitchBuildEvidenceState,
    RunSwitchChangeEffect,
    RunSwitchContainerBuildState,
    RunSwitchMemberState,
    RunSwitchOperationKind,
    RunSwitchPhaseKind,
    RunSwitchProgressState,
    RunSwitchReasonSeverity,
    RunSwitchSubphase,
)

_KNOBS_ADAPTER: TypeAdapter[dict[str, Scalar]] = TypeAdapter(dict[str, Scalar])


_CHANGE_EFFECTS_ADAPTER = TypeAdapter(dict[str, RunSwitchChangeEffect])


_CONTAINER_BUILD_STATE_ADAPTER = TypeAdapter(RunSwitchContainerBuildState)


_BUILD_EVIDENCE_STATE_ADAPTER = TypeAdapter(RunSwitchBuildEvidenceState)


_OPERATION_KIND_ADAPTER = TypeAdapter(RunSwitchOperationKind)


_MEMBER_STATE_ADAPTER = TypeAdapter(RunSwitchMemberState)


_OBSERVING = LifecycleState.OBSERVING.value


_PROGRESS_STATE_ADAPTER = TypeAdapter(RunSwitchProgressState)


_SUBPHASE_ADAPTER = TypeAdapter(RunSwitchSubphase)


_REASON_SEVERITY_ADAPTER = TypeAdapter(RunSwitchReasonSeverity)


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
) -> CatalogDocumentRevision | None:
    """Load only an active canonical Recipe revision for Run/Switch."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    return session.scalar(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == revision_id,
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.state == "active",
        )
    )


_TERMINAL_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
)


_OPERATION_KINDS = frozenset(
    {"recipe.run-switch.v2", "recipe.stop.v2", "recipe.cleanup.v2"}
)


_MEMORY_CAPACITY_REFUSALS = frozenset(
    {
        f"run-switch.{ResourceBlockerCode.INSUFFICIENT_CAPACITY}",
        f"run-switch.{ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP}",
        # Swap in use is a live reading the stopped run's pages are part of:
        # only the post-stop inventory can tell what the stop frees.
        f"run-switch.{ResourceBlockerCode.INSUFFICIENT_SWAP}",
    }
)

#: A swap shortfall among a node's insufficient memory components that only
#: swap in use causes: a reviewed stop of a run holding claims there may
#: clear it (the start re-checks the post-stop inventory).
_SWAP_COMPONENT = ResourceBlockerCode.INSUFFICIENT_SWAP.value
#: A swap shortfall no stop clears (the rise or other loads' held rises
#: exceed the usable swap, or too little swap is active): never stop-covered.
_SWAP_BEYOND_STOP_COMPONENT = f"{_SWAP_COMPONENT}.beyond_stop"


_MEMORY_STOP_CONDITIONAL_REFUSALS = _MEMORY_CAPACITY_REFUSALS | {
    f"run-switch.{ResourceBlockerCode.INSUFFICIENT_RESERVATION_BUDGET}",
    f"run-switch.{ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN}",
}


_INSTALL_PREFLIGHT_REFRESH_REASON = (
    "runtime preflight expired during install compilation"
)


_RUNTIME_IMAGE_OWNER_CHANGED = RunSwitchCode.RUNTIME_IMAGE_OWNER_CHANGED


_RUNTIME_IMAGE_IDENTITY_MISMATCH = (
    RunSwitchCode.RUNTIME_IMAGE_REFERENCE_IDENTITY_MISMATCH
)


_LOGGER = logging.getLogger("vonk-control-run-switch")


_FINAL_VERIFICATION_MAX_SECONDS = 900


_PHASES: tuple[RunSwitchPhaseKind, ...] = (
    "transfer",
    "verify",
    "prepare",
    "cleanup",
    "stop",
    "uninstall",
    "start",
    "final_verify",
)
