"""What a run really used, from the Spark's own memory telemetry.

The agent reports one unified-memory pool per Spark, not per process. While a
single run holds every memory claim on a shared-pool Spark, the memory that
left the pool since that run's admission inventory is that run's: its observed
peak is ``free at admission - lowest available since``. The value is kept on
the run's rank (``RunNode.observed_memory_bytes``) and only ever rises. The
installation's rank keeps the highest of them too
(``InstallationNode.observed_memory_bytes``): a run's history is collected a
day after it ends, its installation's evidence stays while it is installed.

Admission reads it back by content identity (recipe revision digest and role,
on shared-pool Sparks): a peak above the recipe's declaration replaces the
declaration; one below never loosens it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ReservationState
from vonk_agent_protocol.telemetry import HostResourceSample

from .memory_reservations import MEMORY_RESERVATION_KINDS
from .models import (
    CatalogDocumentRevision,
    InstallationNode,
    NodeInventoryResourceSnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .recipe_execution_contract import parse_stored_run_plan
from .settings import PROFILE_STALL_WINDOW_SECONDS


def record_observed_run_memory(
    session: Session,
    node_id: str,
    samples: Sequence[tuple[datetime, int | None]],
) -> None:
    """Raise the sole run's observed peak on ``node_id`` from new samples.

    ``samples`` are ``(observed_at, memory_available_bytes)``. Anything that
    makes the attribution ambiguous (another active claim owner, a separate
    memory pool, a sample older than the admission inventory) records nothing.
    """
    owners = set(
        session.execute(
            select(ResourceReservation.owner_kind, ResourceReservation.owner_id).where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind.in_(MEMORY_RESERVATION_KINDS),
                # A promise holds no memory until its run converts it.
                ResourceReservation.state == ReservationState.ACTIVE,
            )
        ).all()
    )
    if len(owners) != 1:
        return
    ((owner_kind, run_id),) = owners
    run = session.get(RecipeRun, run_id) if owner_kind == "run" else None
    if run is None:
        return
    try:
        plan = parse_stored_run_plan(run.plan)
    except ValueError:
        return
    admitted = next((item for item in plan.nodes if item.node_id == node_id), None)
    rank = session.scalar(
        select(RunNode).where(RunNode.run_id == run.id, RunNode.node_id == node_id)
    )
    if (
        admitted is None
        or rank is None
        or plan.run_generation != run.run_generation
        or admitted.memory_pool != "shared"
        or admitted.available_memory_bytes is None
        or admitted.inventory_observed_at is None
    ):
        return
    baseline_at = datetime.fromisoformat(admitted.inventory_observed_at)
    lowest = min(
        (
            available
            for observed_at, available in samples
            if available is not None and _utc(observed_at) > _utc(baseline_at)
        ),
        default=None,
    )
    if lowest is None:
        return
    peak = admitted.available_memory_bytes - lowest
    if peak > (rank.observed_memory_bytes or 0):
        rank.observed_memory_bytes = peak
    installed = session.scalar(
        select(InstallationNode).where(
            InstallationNode.installation_id == run.installation_id,
            InstallationNode.node_id == node_id,
        )
    )
    if installed is not None and peak > (installed.observed_memory_bytes or 0):
        installed.observed_memory_bytes = peak


def observed_role_peaks(session: Session, content_digest: str) -> dict[str, int]:
    """The highest observed peak per role of runs of this exact recipe content.

    Revision records are read only for their content digest: every revision
    with the same content shares the evidence.
    """

    rows = [
        *session.execute(
            select(
                RunNode.role,
                RunNode.observed_memory_bytes,
                RecipeInstallation.recipe_revision_id,
            )
            .join(RecipeRun, RecipeRun.id == RunNode.run_id)
            .join(
                RecipeInstallation, RecipeInstallation.id == RecipeRun.installation_id
            )
            .where(RunNode.observed_memory_bytes.is_not(None))
        ).all(),
        # What runs observed outlives their history on the installation.
        *session.execute(
            select(
                InstallationNode.role,
                InstallationNode.observed_memory_bytes,
                RecipeInstallation.recipe_revision_id,
            )
            .join(
                RecipeInstallation,
                RecipeInstallation.id == InstallationNode.installation_id,
            )
            .where(InstallationNode.observed_memory_bytes.is_not(None))
        ).all(),
    ]
    digest_of = dict(
        session.execute(
            select(
                CatalogDocumentRevision.id, CatalogDocumentRevision.content_digest
            ).where(CatalogDocumentRevision.id.in_({row[2] for row in rows}))
        ).all()
    )
    peaks: dict[str, int] = {}
    for role, peak, record in rows:
        if (
            peak is not None
            and digest_of.get(record) == content_digest
            and peak > peaks.get(role, -1)
        ):
            peaks[role] = peak
    return peaks


def recent_best_available(
    session: Session, node_id: str, observed_at: datetime | None
) -> int | None:
    """The most memory the Spark reported available over the last stall window.

    One sample is noisy: the host's own use moves by hundreds of megabytes. A
    demand that cannot fit even the best recent sample cannot fit by waiting
    on the host alone; one that fits it may when the host settles again.
    """

    if observed_at is None:
        return None
    since = _utc(observed_at) - timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)
    resources = session.scalars(
        select(NodeInventoryResourceSnapshot.host_resources).where(
            NodeInventoryResourceSnapshot.node_id == node_id,
            NodeInventoryResourceSnapshot.observed_at >= since,
        )
    )
    # Read through the canonical contract, preserving its full byte range and
    # treating damaged evidence as unknown instead of poisoning admission.
    return max(
        (
            item.memory_available_bytes
            for item in resources
            if isinstance(item, HostResourceSample)
            and item.memory_available_bytes is not None
        ),
        default=None,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
