"""One ledger projection for all consumers of physical memory."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol.inventory import MemoryPool

from .models import RecipeBuild, RecipeRun, ResourceReservation, RunNode
from .profile_capacity import (
    profile_build_memory_claims,
    profile_memory_floor,
    profile_memory_replacements,
    reservation_visible,
)
from .recipe_execution_contract import parse_stored_run_plan
from .resource_planning import (
    MemoryReservationTotals,
    UnknownRunMemoryResidual,
    memory_reservation_kinds,
)
from .run_switch_contract import StopImpact

MEMORY_RESERVATION_KINDS = ("host-memory", "gpu-memory", "unified-memory")


def memory_reserve_floor(
    session: Session, node_id: str, *, memory_pool: MemoryPool | None, kind: str
) -> int:
    """Keep the largest declared reserve for consumers of this physical pool."""
    if memory_pool is None:
        raise ValueError("memory pool evidence is unavailable")
    floor = 0
    for claim in session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.node_id == node_id,
            ResourceReservation.kind.in_(memory_reservation_kinds(kind, memory_pool)),
            ResourceReservation.state.in_(("active", "promised")),
        )
    ):
        if claim.owner_kind == "fleet-profile":
            floor = max(floor, profile_memory_floor(session, claim))
        elif claim.owner_kind == "run":
            run = session.get(RecipeRun, claim.owner_id)
            if run is None or run.plan_digest != claim.plan_digest:
                raise ValueError("run memory reservation owner is missing or changed")
            plan = parse_stored_run_plan(run.plan)
            node = next((item for item in plan.nodes if item.node_id == node_id), None)
            if node is None or node.memory_pool != memory_pool:
                raise ValueError("run memory reservation pool is missing or changed")
            floor = max(floor, node.memory_floor_bytes)
    return floor


def reviewed_run_memory_reservations(
    session: Session,
    node_id: str,
    stop: StopImpact,
) -> tuple[ResourceReservation, ...]:
    if node_id not in stop.node_ids:
        return ()
    return tuple(
        session.scalars(
            select(ResourceReservation)
            .join(RecipeRun, RecipeRun.id == ResourceReservation.owner_id)
            .where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == stop.run_id,
                ResourceReservation.kind.in_(MEMORY_RESERVATION_KINDS),
                ResourceReservation.state == "active",
                ResourceReservation.plan_digest == stop.run_plan_digest,
                RecipeRun.plan_digest == stop.run_plan_digest,
            )
        )
    )


def memory_reservations(
    session: Session,
    node_id: str,
    *,
    memory_pool: MemoryPool | None,
    excluded_profile_application_ids: Sequence[str] = (),
    excluded_profile_claim_ids: Sequence[str] = (),
    excluded_run_ids: Sequence[str] = (),
) -> MemoryReservationTotals:
    claims = tuple(
        session.scalars(
            select(ResourceReservation)
            .where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind.in_(MEMORY_RESERVATION_KINDS),
                ResourceReservation.state.in_(("active", "promised")),
                reservation_visible(
                    excluded_profile_application_ids, excluded_run_ids=excluded_run_ids
                ),
                ResourceReservation.id.not_in(excluded_profile_claim_ids),
            )
            .order_by(ResourceReservation.id)
        )
    )
    usage = {claim.id: _claim_usage(session, claim) for claim in claims}
    if memory_pool is None:
        committed = {
            kind: sum(claim.amount_bytes for claim in claims if claim.kind == kind)
            for kind in MEMORY_RESERVATION_KINDS
        }
        unmaterialized = {
            kind: sum(
                claim.amount_bytes
                for claim in claims
                if claim.kind == kind and usage[claim.id].unmaterialized
            )
            for kind in MEMORY_RESERVATION_KINDS
        }
        unknown = {
            kind: tuple(
                projection.unknown_residual
                for claim in claims
                if claim.kind == kind
                if (projection := usage[claim.id]).unknown_residual is not None
            )
            for kind in MEMORY_RESERVATION_KINDS
        }
        return MemoryReservationTotals(
            {kind: amount for kind, amount in committed.items() if amount},
            {kind: amount for kind, amount in unmaterialized.items() if amount},
            {kind: value for kind, value in unknown.items() if value},
        )
    active = [claim for claim in claims if claim.state == "active"]
    future: dict[str, list[ResourceReservation]] = {}
    for claim in claims:
        if claim.state == "promised":
            future.setdefault(claim.owner_id, []).append(claim)
    # A future replacement and the exact runs it will stop occupy successive
    # phases. Account for the larger reservation envelope, retaining the live
    # owner's row until its stop receipt releases it. This is ledger overlap,
    # not an estimate of physically freed bytes or observed process usage.
    replacements = {
        owner: {
            old.id
            for claim in promised
            for stop in profile_memory_replacements(session, claim)
            for old in reviewed_run_memory_reservations(session, node_id, stop)
        }
        for owner, promised in future.items()
    }
    # A preparation build and its future runtime are sequential users of the
    # same accepted claim. Include only the exact current build consumer; an
    # independent build or stale parent cannot supply this overlap.
    for build_claim in active:
        if build_claim.owner_kind != "recipe-build":
            continue
        build = session.get(RecipeBuild, build_claim.owner_id)
        if (
            build is None
            or build.state != "building"
            or build.build_input_sha256 != build_claim.plan_digest
            or build.builder_node_id != node_id
        ):
            continue
        for inherited in profile_build_memory_claims(
            session, build, memory_pool=memory_pool
        ):
            if inherited.owner_id in replacements:
                replacements[inherited.owner_id].add(build_claim.id)
    committed_totals: dict[str, int] = {}
    unmaterialized_totals: dict[str, int] = {}
    unknown_totals: dict[str, tuple[UnknownRunMemoryResidual, ...]] = {}
    for pool_kind in (
        ("unified-memory",)
        if memory_pool == "shared"
        else ("host-memory", "gpu-memory")
    ):
        overlapping = memory_reservation_kinds(pool_kind, memory_pool)
        live = [claim for claim in active if claim.kind in overlapping]
        total = sum(claim.amount_bytes for claim in live)
        unmaterialized = sum(
            claim.amount_bytes for claim in live if usage[claim.id].unmaterialized
        )
        unknown = tuple(
            projection.unknown_residual
            for claim in live
            if (projection := usage[claim.id]).unknown_residual is not None
        )
        credited: set[str] = set()
        for owner, promised in future.items():
            demand = sum(
                claim.amount_bytes for claim in promised if claim.kind in overlapping
            )
            replaced = [
                claim
                for claim in live
                if claim.id not in credited and claim.id in replacements[owner]
            ]
            increment = max(0, demand - sum(claim.amount_bytes for claim in replaced))
            total += increment
            replaced_unmaterialized = sum(
                claim.amount_bytes
                for claim in replaced
                if usage[claim.id].unmaterialized
            )
            # A promise replacing a live run is still a future allocation.
            # Only another not-yet-materialized claim can supply overlap here;
            # aggregate free already includes the live run's current use, not
            # its reserved peak or the promised replacement's future demand.
            unmaterialized += max(0, demand - replaced_unmaterialized)
            credited.update(claim.id for claim in replaced)
        if total:
            committed_totals[pool_kind] = total
        if unmaterialized:
            unmaterialized_totals[pool_kind] = unmaterialized
        if unknown:
            unknown_totals[pool_kind] = unknown
    return MemoryReservationTotals(
        committed_totals, unmaterialized_totals, unknown_totals
    )


@dataclass(frozen=True, slots=True)
class _ClaimUsage:
    unmaterialized: bool
    unknown_residual: UnknownRunMemoryResidual | None = None


def _claim_usage(session: Session, claim: ResourceReservation) -> _ClaimUsage:
    """Classify one claim without treating run state as resident-byte evidence."""
    if claim.owner_kind == "fleet-profile":
        return _ClaimUsage(unmaterialized=True)
    if claim.owner_kind == "recipe-build":
        # The builder's process is external to this SQL receipt. Keep its
        # committed envelope against observed free until the build owner
        # releases it; hard pool limits still count it only once.
        return _ClaimUsage(unmaterialized=True)
    if claim.owner_kind != "run":
        raise ValueError("memory reservation has an unsupported owner kind")
    run = session.get(RecipeRun, claim.owner_id)
    if run is None:
        return _ClaimUsage(unmaterialized=True)
    run_node = session.scalar(
        select(RunNode).where(
            RunNode.run_id == claim.owner_id,
            RunNode.node_id == claim.node_id,
        )
    )
    if run_node is None:
        return _ClaimUsage(unmaterialized=True)
    # The active SQL claim has no connected per-run usage producer. Lifecycle
    # state, including "starting" and "running", cannot narrow its possible
    # residual. Inventory already reflects an unknown amount in [0, peak], so
    # preserve that range separately and use its full upper bound for safety.
    return _ClaimUsage(
        unmaterialized=False,
        unknown_residual=UnknownRunMemoryResidual(
            run_id=run.id,
            run_generation=run.run_generation,
            reservation_kind=claim.kind,
            maximum_bytes=claim.amount_bytes,
        ),
    )
