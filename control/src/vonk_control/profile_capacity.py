from vonk_agent_protocol import (
    DesiredAssignmentState,
    ReservationState,
    RunSwitchCode,
    UnknownOutcomeError,
    WaitReason,
)

"""Capacity ownership from accepted profile intent to its exact child.

The ordinary ResourceReservation row is the claim. Handoff changes its owner
in the installation transaction; it neither copies the claim nor opens a gap.
Only unassigned profile claims are released when their application terminates.

Release and restore are one pair: an application that fails releases its
unassigned claims, and the same application returning to work (its live child
resumed) takes them back. A disk claim is bookkeeping for a promise, never a
precondition of the work: a live application whose disk claim is absent,
released or stale reserves again under the reservation locks and the ordinary
capacity check instead of failing. Port and memory promises are exact-plan
authority and stay strict.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_agent_protocol.inventory import MemoryPool

from . import job_states
from .fleet_profile_contract import (
    FLEET_PROFILE_ENDED_STATES,
    FleetProfileApplicationProgress,
    FleetProfileAssignment,
    FleetProfilePreview,
    FleetProfileResourceRequirement,
)
from .models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from .preparation_contract import RuntimeImageIdentity
from .resource_planning import memory_reservation_kind
from .run_switch_contract import RunSwitchOperationResult, RunSwitchPlan, StopImpact
from .strict_json import read_stored_model


def reservation_visible(
    excluded_profile_application_ids: Sequence[str] = (),
    *,
    excluded_run_ids: Sequence[str] = (),
    excluded_installation_ids: Sequence[str] = (),
) -> ColumnElement[bool]:
    """Only an explicit inherited, superseded or adoptable claim is discounted."""
    return and_(
        or_(
            ResourceReservation.owner_kind != "fleet-profile",
            ResourceReservation.owner_id.not_in(excluded_profile_application_ids),
        ),
        or_(
            ResourceReservation.owner_kind != "run",
            ResourceReservation.owner_id.not_in(excluded_run_ids),
        ),
        or_(
            ResourceReservation.owner_kind != "installation",
            ResourceReservation.owner_id.not_in(excluded_installation_ids),
        ),
    )


def reserve_profile_disk(
    session: Session,
    application: FleetProfileApplication,
    review: FleetProfilePreview,
    assignment_ids: set[str],
    *,
    now: datetime,
) -> None:
    for decision in review.admission_decisions:
        if decision.assignment_id not in assignment_ids:
            continue
        for node in decision.requirements:
            if node.disk_required_bytes is None:
                raise ValueError("profile disk requirement is unavailable")
            session.add(
                ResourceReservation(
                    id=_disk_claim_id(
                        application.id, decision.assignment_id, node.node_id
                    ),
                    node_id=node.node_id,
                    kind="disk",
                    resource_key=decision.assignment_id,
                    amount_bytes=node.disk_required_bytes,
                    owner_kind="fleet-profile",
                    owner_id=application.id,
                    state=ReservationState.ACTIVE,
                    plan_digest=application.plan_digest,
                    created_at=now,
                )
            )


def _disk_claim_id(application_id: str, assignment_id: str, node_id: str) -> str:
    return str(
        uuid5(UUID(application_id), f"profile-capacity:disk:{assignment_id}:{node_id}")
    )


def _port_claim_id(
    application_id: str, assignment_id: str, node_id: str, port: int
) -> str:
    return str(
        uuid5(
            UUID(application_id),
            f"profile-capacity:port:{assignment_id}:{node_id}:{port}",
        )
    )


def _memory_claim_id(application_id: str, assignment_id: str, node_id: str) -> str:
    return str(
        uuid5(
            UUID(application_id), f"profile-capacity:memory:{assignment_id}:{node_id}"
        )
    )


def reserve_profile_memory(
    session: Session,
    application: FleetProfileApplication,
    review: FleetProfilePreview,
    assignment_ids: set[str],
    *,
    now: datetime,
) -> None:
    for decision in review.admission_decisions:
        if decision.assignment_id not in assignment_ids:
            continue
        for node in decision.requirements:
            if (
                node.memory_required_bytes is None
                or node.memory_kind is None
                or node.memory_pool is None
            ):
                raise ValueError("profile memory requirement is unavailable")
            session.add(
                ResourceReservation(
                    id=_memory_claim_id(
                        application.id, decision.assignment_id, node.node_id
                    ),
                    node_id=node.node_id,
                    kind=memory_reservation_kind(node.memory_kind),
                    resource_key=decision.assignment_id,
                    amount_bytes=node.memory_required_bytes,
                    owner_kind="fleet-profile",
                    owner_id=application.id,
                    state=ReservationState.PROMISED,
                    plan_digest=application.plan_digest,
                    created_at=now,
                )
            )


def _validate_memory_claim(
    claim: ResourceReservation,
    application: FleetProfileApplication,
    assignment_id: str,
    requirement: FleetProfileResourceRequirement,
) -> None:
    if (
        requirement.memory_kind is None
        or requirement.memory_pool is None
        or requirement.memory_required_bytes is None
        or claim.id
        != _memory_claim_id(application.id, assignment_id, requirement.node_id)
        or claim.node_id != requirement.node_id
        or claim.resource_key != assignment_id
        or claim.kind != memory_reservation_kind(requirement.memory_kind)
        or claim.amount_bytes != requirement.memory_required_bytes
        or claim.owner_kind != "fleet-profile"
        or claim.owner_id != application.id
        or claim.state != ReservationState.PROMISED
        or claim.plan_digest != application.plan_digest
    ):
        raise ValueError("profile memory claim is missing or changed")


def inherited_profile_memory(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    alias: str,
    node_memory: Mapping[str, tuple[str, int, MemoryPool | None]],
    *,
    workload_intent_ordinal: int | None,
) -> dict[str, ResourceReservation]:
    application, assignment, requirements = _profile_assignment(
        session,
        application_id,
        recipe_revision_id,
        tuple(node_memory),
        workload_intent_ordinal=workload_intent_ordinal,
    )
    if (
        assignment.desired_state != DesiredAssignmentState.RUNNING
        or assignment.alias != alias
    ):
        raise ValueError("run is outside the profile memory claim")
    claims = {}
    for requirement in requirements:
        if node_memory[requirement.node_id] != (
            requirement.memory_kind,
            requirement.memory_required_bytes,
            requirement.memory_pool,
        ):
            raise ValueError("run memory changed after profile review")
        claim = session.get(
            ResourceReservation,
            _memory_claim_id(application_id, assignment.id, requirement.node_id),
        )
        if claim is None:
            raise ValueError("profile memory claim is missing or changed")
        _validate_memory_claim(claim, application, assignment.id, requirement)
        claims[requirement.node_id] = claim
    return claims


def profile_memory_replacements(
    session: Session,
    claim: ResourceReservation,
) -> tuple[StopImpact, ...]:
    """Only exact reviewed run generations may overlap a future memory claim."""
    application = session.get(FleetProfileApplication, claim.owner_id)
    if application is None:
        raise ValueError("profile memory claim owner is missing")
    review = read_stored_model(
        FleetProfilePreview,
        canonical_message(application.plan),
        strict=True,
        from_json=True,
    )
    progress = read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(application.progress),
        strict=True,
        from_json=True,
    )
    decision = next(
        (
            item
            for item in review.admission_decisions
            if item.assignment_id == claim.resource_key
        ),
        None,
    )
    requirement = (
        next(
            (item for item in decision.requirements if item.node_id == claim.node_id),
            None,
        )
        if decision is not None
        else None
    )
    if decision is None or requirement is None:
        raise ValueError("profile memory requirement is missing or changed")
    _validate_memory_claim(claim, application, decision.assignment_id, requirement)
    node = session.get(AgentNode, claim.node_id)
    if (
        application.state
        not in job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
        )
        or node is None
        or progress.workload_intent_ordinal is None
        or node.workload_intent_ordinal != progress.workload_intent_ordinal
    ):
        return ()
    return tuple(stop for stop in decision.stops if claim.node_id in stop.node_ids)


def profile_build_memory_claims(
    session: Session,
    build: RecipeBuild,
    *,
    memory_pool: MemoryPool | None,
    request_id: str | None = None,
    lock: bool = False,
) -> tuple[ResourceReservation, ...]:
    """Derive temporary build use from the accepted parent, never from a name.

    Admission supplies the deterministic child request key. The ledger omits
    it to observe all current consumers of an already admitted exact build.
    The promise remains parent-owned until the runtime's atomic handoff.
    """
    claims: dict[str, ResourceReservation] = {}
    query = (
        select(Job)
        .where(
            Job.kind == "recipe.run-switch.v2",
            Job.payload["plan"]["build"]["build_id"].as_string() == build.id,
        )
        .order_by(Job.id)
    )
    if request_id is None:
        query = query.where(Job.state.in_(("queued", "running")))
    candidates = tuple(session.scalars(query))
    for candidate in candidates:
        if request_id is not None and request_id != str(
            uuid5(UUID(candidate.request_id), "container-build")
        ):
            continue
        job = session.get(
            Job,
            candidate.id,
            populate_existing=True,
            with_for_update={"nowait": True} if lock else None,
        )
        if job is None:
            continue
        try:
            claim = _profile_build_memory_claim(
                session, job, build, memory_pool=memory_pool, lock=lock
            )
        except ValueError:
            if request_id is not None:
                raise
            # Invalid/stale consumer evidence cannot discount capacity. A
            # ledger read conservatively counts both owners rather than
            # blocking unrelated work on another consumer's history.
            continue
        if claim is not None:
            claims[claim.id] = claim
    return tuple(claims.values())


def _profile_build_memory_claim(
    session: Session,
    job: Job,
    build: RecipeBuild,
    *,
    memory_pool: MemoryPool | None,
    lock: bool,
) -> ResourceReservation | None:
    if job.state not in {"queued", "running"}:
        raise ValueError("profile build consumer is no longer active")
    plan = read_stored_model(
        RunSwitchPlan,
        canonical_message(job.payload["plan"]),
        strict=True,
        from_json=True,
    )
    progress = read_stored_model(
        RunSwitchOperationResult,
        canonical_message(job.result),
        strict=True,
        from_json=True,
    )
    if progress.profile_application_id is None:
        return None
    if progress.cancellation is not None:
        raise ValueError("profile build consumer was cancelled")
    if progress.phase_index >= len(plan.phases):
        raise ValueError("profile build consumer phase is complete")
    phase = plan.phases[progress.phase_index]
    if (phase.kind, phase.subphase, phase.state) != (
        "prepare",
        "container-build",
        "planned",
    ):
        raise ValueError("profile build consumer is outside preparation")
    if plan.recipe_revision_id is None:
        raise ValueError("profile build consumer revision is unavailable")
    if (
        plan.recipe_build_id != build.id
        or plan.build.build_input_sha256 != build.build_input_sha256
        or plan.build.builder_node_id != build.builder_node_id
    ):
        raise ValueError("profile build identity changed after review")
    nodes = tuple(node.node_id for node in plan.spark_group.nodes)
    if build.builder_node_id not in nodes:
        return None
    if lock:
        session.get(
            FleetProfileApplication,
            progress.profile_application_id,
            populate_existing=True,
            with_for_update={"nowait": True},
        )
    application, assignment, requirements = _profile_assignment(
        session,
        progress.profile_application_id,
        plan.recipe_revision_id,
        nodes,
        workload_intent_ordinal=progress.workload_intent_ordinal,
    )
    image = accepted_profile_runtime_image(
        session, application.id, plan.recipe_revision_id, nodes
    )
    if image.build_id != build.id or assignment.alias != plan.alias:
        raise ValueError("build is outside the reviewed profile preparation")
    if assignment.desired_state != DesiredAssignmentState.RUNNING:
        return None
    requirement = next(
        item for item in requirements if item.node_id == build.builder_node_id
    )
    if requirement.memory_pool != memory_pool:
        raise ValueError("profile build memory pool changed after review")
    claim = session.get(
        ResourceReservation,
        _memory_claim_id(application.id, assignment.id, build.builder_node_id),
        populate_existing=True,
        with_for_update={"nowait": True} if lock else None,
    )
    if claim is None:
        raise ValueError("profile build memory claim is missing")
    _validate_memory_claim(claim, application, assignment.id, requirement)
    return claim


def profile_memory_floor(session: Session, claim: ResourceReservation) -> int:
    """The accepted recipe reserve survives temporary preparation use."""
    application = session.get(FleetProfileApplication, claim.owner_id)
    if application is None:
        raise ValueError("profile memory claim owner is missing")
    review = read_stored_model(
        FleetProfilePreview,
        canonical_message(application.plan),
        strict=True,
        from_json=True,
    )
    requirement = next(
        (
            node
            for decision in review.admission_decisions
            if decision.assignment_id == claim.resource_key
            for node in decision.requirements
            if node.node_id == claim.node_id
        ),
        None,
    )
    if requirement is None or requirement.memory_floor_bytes is None:
        raise ValueError("profile memory requirement is missing")
    _validate_memory_claim(claim, application, claim.resource_key, requirement)
    return requirement.memory_floor_bytes


def reserve_profile_ports(
    session: Session,
    application: FleetProfileApplication,
    review: FleetProfilePreview,
    assignment_ids: set[str],
    *,
    now: datetime,
) -> None:
    for decision in review.admission_decisions:
        if decision.assignment_id not in assignment_ids:
            continue
        for node in decision.requirements:
            for port in node.ports_required:
                session.add(
                    ResourceReservation(
                        id=_port_claim_id(
                            application.id, decision.assignment_id, node.node_id, port
                        ),
                        node_id=node.node_id,
                        kind="port",
                        resource_key=str(port),
                        amount_bytes=0,
                        owner_kind="fleet-profile",
                        owner_id=application.id,
                        state=ReservationState.PROMISED,
                        plan_digest=application.plan_digest,
                        created_at=now,
                    )
                )


def inherited_profile_ports(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    alias: str,
    node_ports: Mapping[str, Sequence[int]],
    *,
    workload_intent_ordinal: int | None,
) -> dict[tuple[str, int], ResourceReservation]:
    """Caller holds node/reservation locks; the promise is consumed atomically."""
    application, assignment, requirements = _profile_assignment(
        session,
        application_id,
        recipe_revision_id,
        tuple(node_ports),
        workload_intent_ordinal=workload_intent_ordinal,
    )
    if (
        assignment.desired_state != DesiredAssignmentState.RUNNING
        or assignment.alias != alias
    ):
        raise ValueError("run is outside the profile port claim")
    expected = {
        (node.node_id, port) for node in requirements for port in node.ports_required
    }
    actual = {
        (node_id, port) for node_id, ports in node_ports.items() for port in ports
    }
    if actual != expected:
        raise ValueError("run ports changed after profile review")
    ids = {
        _port_claim_id(application_id, assignment.id, node_id, port): (node_id, port)
        for node_id, port in expected
    }
    claims = {
        claim.id: claim
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.id.in_(ids))
        )
    }
    if set(claims) != set(ids) or any(
        claim.owner_kind != "fleet-profile"
        or claim.owner_id != application_id
        or claim.kind != "port"
        or claim.state != ReservationState.PROMISED
        or claim.node_id != ids[claim.id][0]
        or claim.resource_key != str(ids[claim.id][1])
        or claim.plan_digest != application.plan_digest
        or claim.amount_bytes != 0
        for claim in claims.values()
    ):
        raise ValueError("profile port claim is missing or changed")
    return {ids[claim_id]: claim for claim_id, claim in claims.items()}


def _profile_assignment(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    node_ids: Sequence[str],
    *,
    workload_intent_ordinal: int | None,
) -> tuple[
    FleetProfileApplication,
    FleetProfileAssignment,
    tuple[FleetProfileResourceRequirement, ...],
]:
    application = session.get(FleetProfileApplication, application_id)
    if application is None or application.state not in job_states.words(
        LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
    ):
        raise ValueError("profile capacity owner is no longer active")
    progress = read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(application.progress),
        strict=True,
        from_json=True,
    )
    review = read_stored_model(
        FleetProfilePreview,
        canonical_message(application.plan),
        strict=True,
        from_json=True,
    )
    if (
        workload_intent_ordinal is None
        or progress.workload_intent_ordinal != workload_intent_ordinal
    ):
        raise ValueError("profile capacity owner does not match workload intent")
    nodes = tuple(sorted(node_ids))
    current = tuple(
        session.execute(
            select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
            .where(AgentNode.node_id.in_(nodes))
            .order_by(AgentNode.node_id)
        )
    )
    if current != tuple((node_id, workload_intent_ordinal) for node_id in nodes):
        raise ValueError("profile capacity intent was superseded")
    assignments = [
        item
        for item in review.resolved_assignments
        if item.recipe_revision_id == recipe_revision_id
        and tuple(sorted(node.node_id for node in item.nodes)) == nodes
    ]
    if len(assignments) != 1:
        raise ValueError("installation is outside the profile capacity claim")
    assignment = assignments[0]
    requirements = tuple(
        node
        for item in review.admission_decisions
        if item.assignment_id == assignment.id
        for node in item.requirements
    )
    if {node.node_id for node in requirements} != set(nodes):
        raise ValueError("profile capacity requirement is missing or changed")
    return application, assignment, requirements


def accepted_profile_runtime_image(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    node_ids: Sequence[str],
) -> RuntimeImageIdentity:
    """Read the original image promise under the current assignment authority.

    Missing managed bytes may require repair; they never erase the accepted
    image identity or authorize replacement with a different build result.
    """
    # Keep intent/reference validation with its existing owner. Import at the
    # call boundary because profile orchestration also consumes capacity helpers.
    from .fleet_profiles import FleetProfileConflict, FleetProfileService
    from .lifecycle.evidence import Residue

    application = session.get(FleetProfileApplication, application_id)
    if application is None:
        raise ValueError("profile image owner is unavailable")
    progress = read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(application.progress),
        strict=True,
        from_json=True,
    )
    application, assignment, _ = _profile_assignment(
        session,
        application_id,
        recipe_revision_id,
        node_ids,
        workload_intent_ordinal=progress.workload_intent_ordinal,
    )
    try:
        intended = FleetProfileService._intended_profile(application, session=session)
    except FleetProfileConflict as error:
        raise ValueError(str(error)) from error
    if isinstance(intended, Residue):
        # (A ValueError like its siblings: the caller maps that to its own refusal.)
        raise ValueError(  # noqa: TRY004
            intended.note or "profile image intent is unavailable"
        )
    root = session.get(FleetProfileApplication, intended.reviewed_application_id)
    if root is None:
        raise ValueError("profile image review source is unavailable")
    review = read_stored_model(
        FleetProfilePreview, canonical_message(root.plan), strict=True, from_json=True
    )
    decisions = [
        item
        for item in review.preparation_decisions
        if item.assignment_id == assignment.id
    ]
    if len(decisions) != 1:
        raise ValueError("profile image identity is missing or ambiguous")
    return decisions[0].runtime_image


def _profile_disk_binding(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    node_ids: Sequence[str],
    *,
    workload_intent_ordinal: int | None,
) -> tuple[
    FleetProfileApplication, str, dict[str, int | None], dict[str, ResourceReservation]
]:
    """The accepted requirement and whichever deterministic claim rows exist.

    A claim is bookkeeping for a promise, never a precondition of the work: a
    row that is absent (never reserved, or released with the installation that
    held it) is simply not returned, and the caller reserves afresh.
    """
    application, assignment, required = _profile_assignment(
        session,
        application_id,
        recipe_revision_id,
        node_ids,
        workload_intent_ordinal=workload_intent_ordinal,
    )
    assignment_id = assignment.id
    nodes = tuple(sorted(node_ids))
    requirements = {node.node_id: node.disk_required_bytes for node in required}
    ids = {_disk_claim_id(application_id, assignment_id, node_id) for node_id in nodes}
    claims = {
        claim.node_id: claim
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.id.in_(ids))
        )
        if claim.kind == "disk" and claim.node_id in nodes
    }
    return application, assignment_id, requirements, claims


def inherited_profile_disk(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    node_ids: Sequence[str],
    *,
    workload_intent_ordinal: int | None,
    now: datetime,
) -> dict[str, ResourceReservation]:
    """The claims the installation may take over, by node; others are reserved afresh.

    Caller holds the target-node and reservation locks. Do not lock the parent:
    its scheduler can already hold that row while admitting a child. Release
    and handoff serialize on the reservation row itself.

    Only a claim that still holds exactly the reviewed promise is inherited. A
    claim that is gone, was released with an earlier installation, or has
    drifted from the current plan never blocks the live application: a
    drifted one is released here, and admission reserves the node again under
    the same locks and the ordinary capacity check, so a shortage waits with
    its named reason instead of failing on bookkeeping.
    """
    application, assignment_id, requirements, found = _profile_disk_binding(
        session,
        application_id,
        recipe_revision_id,
        node_ids,
        workload_intent_ordinal=workload_intent_ordinal,
    )
    claims: dict[str, ResourceReservation] = {}
    for node_id, claim in found.items():
        if (
            claim.state == ReservationState.ACTIVE
            and claim.owner_kind == "fleet-profile"
            and claim.owner_id == application_id
        ):
            if (
                claim.resource_key == assignment_id
                and claim.plan_digest == application.plan_digest
                and claim.amount_bytes == requirements.get(node_id)
            ):
                claims[node_id] = claim
            else:
                claim.state = ReservationState.RELEASED
                claim.released_at = now
    return claims


def prepared_profile_installation(
    session: Session,
    application_id: str,
    recipe_revision_id: str,
    node_ids: Sequence[str],
    *,
    workload_intent_ordinal: int,
) -> tuple[str, str] | None:
    """Find the exact handoff after a lost parent checkpoint, before re-admission.

    ``None`` means nothing is handed off: the promise is still the profile's,
    or a claim was released or never existed (the caller admits the
    installation as usual and reserves afresh). Active claims that disagree
    about their owner cannot prove the atomic handoff. They remain accounted
    to their existing owners; a typed unknown outcome triggers bounded reconciliation.
    """
    _, _, requirements, claims = _profile_disk_binding(
        session,
        application_id,
        recipe_revision_id,
        node_ids,
        workload_intent_ordinal=workload_intent_ordinal,
    )
    if set(claims) != set(node_ids) or any(
        claim.state != ReservationState.ACTIVE for claim in claims.values()
    ):
        return None
    if all(
        claim.owner_kind == "fleet-profile" and claim.owner_id == application_id
        for claim in claims.values()
    ):
        return None
    identities = {(claim.owner_id, claim.plan_digest) for claim in claims.values()}
    if len(identities) != 1 or any(
        claim.owner_kind != "installation"
        or requirements.get(node_id) is None
        or claim.amount_bytes > (requirements[node_id] or 0)
        for node_id, claim in claims.items()
    ):
        # Active ownership is authority, not a disposable admission shortcut.
        raise UnknownOutcomeError(
            RunSwitchCode.INSTALLATION_HANDOFF_INCONSISTENT,
            reason=WaitReason.SCOPE_CHANGED,
        )
    return identities.pop()


def release_replaced_profile_claims(
    session: Session,
    application: FleetProfileApplication,
    *,
    node_ids: Sequence[str],
    now: datetime,
) -> None:
    """Release only unconsumed promises on siblings fenced by newer intent.

    Continuing nodes retain their original owner. Materialized claims have
    changed owner under the consumer's atomic handoff and are untouched.
    """
    for claim in session.scalars(
        select(ResourceReservation)
        .where(
            ResourceReservation.owner_kind == "fleet-profile",
            ResourceReservation.owner_id == application.id,
            ResourceReservation.node_id.in_(node_ids),
            ResourceReservation.state.in_(
                (ReservationState.ACTIVE, ReservationState.PROMISED)
            ),
        )
        .order_by(ResourceReservation.node_id, ResourceReservation.id)
        .with_for_update(nowait=True)
    ):
        claim.state = ReservationState.RELEASED
        claim.released_at = now


def release_unassigned_profile_claims(
    session: Session, application: FleetProfileApplication, *, now: datetime
) -> None:
    if application.state not in FLEET_PROFILE_ENDED_STATES:
        return
    for claim in session.scalars(
        select(ResourceReservation)
        .where(
            ResourceReservation.owner_kind == "fleet-profile",
            ResourceReservation.owner_id == application.id,
            ResourceReservation.state.in_(
                (ReservationState.ACTIVE, ReservationState.PROMISED)
            ),
        )
        .order_by(ResourceReservation.id)
        .with_for_update(nowait=True)
    ):
        claim.state = ReservationState.RELEASED
        claim.released_at = now


def restore_released_profile_claims(
    session: Session,
    application: FleetProfileApplication,
    *,
    node_ids: Sequence[str] | None = None,
) -> None:
    """Take back the claims an application released while it was failed.

    The inverse of ``release_unassigned_profile_claims``, for the same
    application when its live child is advanced again. Only rows that still
    carry the accepted plan return. A claim consumed by an installation or a
    run changed owner and is untouched. A port another load promised in the
    meantime stays released (the unique promise index refuses it): the run
    path then names the conflict instead of this restoring a second owner.
    Consumption re-checks real capacity, so a restored claim never admits more
    than the ordinary admission would.
    """
    if application.state not in job_states.words(
        LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
    ):
        return
    claims = select(ResourceReservation).where(
        ResourceReservation.owner_kind == "fleet-profile",
        ResourceReservation.owner_id == application.id,
        ResourceReservation.state == ReservationState.RELEASED,
        ResourceReservation.plan_digest == application.plan_digest,
    )
    if node_ids is not None:
        claims = claims.where(ResourceReservation.node_id.in_(node_ids))
    for claim in session.scalars(
        claims.order_by(ResourceReservation.id).with_for_update(nowait=True)
    ):
        try:
            with session.begin_nested():
                claim.state = (
                    ReservationState.ACTIVE
                    if claim.kind == "disk"
                    else ReservationState.PROMISED
                )
                claim.released_at = None
                session.flush()
        except IntegrityError:
            continue
