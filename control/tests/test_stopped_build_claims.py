"""A build's claims never outlive the build.

Fault injection of the production shape: a build cancelled after its order was
issued (attempt > 0) queues one Spark cleanup; when that cleanup fails with an
unconfirmed effect, the build Job stays open (its cancel in flight) and nothing
issues another cleanup. Once the build stopped and its own Spark orders ended,
its memory claim holds nothing on the Spark (whatever it left is in observed
free memory): admission there must not count it, and the next reconcile
releases its claims.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.memory_reservations import memory_reservations
from vonk_control.models import Job, ResourceReservation
from vonk_control.owner_liveness import OwnerKind
from vonk_control.reservation_owners import OWNER_SETTLE_GRACE, reservation_owner

from .test_build_cancellation_recovery import _issue, _services
from .test_recipe_builds import setup


def _counted(sessions, node_id):
    with sessions() as session:
        totals = memory_reservations(session, node_id, memory_pool="separate")
    return sum(totals.unmaterialized_bytes_by_kind.values())


def _held_kinds(sessions, build_id):
    with sessions() as session:
        return set(
            session.scalars(
                select(ResourceReservation.kind).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == build_id,
                    ResourceReservation.state == "active",
                )
            )
        )


def _reconcile(sessions, at):
    AttemptResidueReconciler(
        sessions, abandon_never_installed=lambda _id: None, clock=lambda: at
    ).tick()


def stopped_build_releases_its_memory(tmp_path, engine):
    """Cancel a build mid-run, fail its cleanup; return what a fresh build got.

    The cancelled build's memory claim is widened to all the Spark's free
    memory, so a fresh build there is admitted only if that claim is not
    counted.
    """

    sessions, builds, operations, _storage, now, node_id, _revision, plan = _services(
        tmp_path, engine
    )
    cancelled = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    _issue(sessions, cancelled.id)
    with sessions.begin() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == plan.build_id,
                ResourceReservation.kind == "host-memory",
            )
        )
        assert claim is not None
        claim.amount_bytes = 80_000
    operations.cancel(
        cancelled.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel this preparation",
    )
    # The cleanup is still stopping the build: its memory still counts.
    assert _counted(sessions, node_id) == 80_000
    _reconcile(sessions, now + OWNER_SETTLE_GRACE)
    assert _held_kinds(sessions, plan.build_id) == {"disk", "host-memory"}

    with sessions() as session:
        cleanup = session.scalar(
            select(Job.id).where(Job.kind == "recipe.build.cleanup.v1")
        )
    assert cleanup is not None
    operations.record_node_result(
        cleanup,
        node_id,
        succeeded=False,
        evidence={"reason": "build effects are not yet confirmed quiescent"},
    )
    operations.record_node_result(cancelled.id, node_id, succeeded=False, evidence={})
    with sessions() as session:
        parent = session.get(Job, cancelled.id)
        assert parent is not None and parent.state == "running"
    assert operations.reconcile_cancelled_builds() is False

    assert _counted(sessions, node_id) == 0
    _sessions, _bundles, _later, _node, other = setup(
        tmp_path,
        engine=engine,
        existing_node=True,
        builder_node_id=node_id,
        recipe_slug="other-vllm",
    )
    other_plan = builds.plan(other.id, node_id, now=now)
    fresh = operations.build(
        other_plan,
        build_input_sha256=other_plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )

    later = now + OWNER_SETTLE_GRACE
    with sessions() as session:
        owner = reservation_owner(session, "recipe-build", plan.build_id, now=later)
    assert owner.dead and "orders have ended" in owner.describe()
    _reconcile(sessions, later)
    assert _held_kinds(sessions, plan.build_id) == set()
    assert _held_kinds(sessions, other_plan.build_id) == {"disk", "host-memory"}
    return fresh


def test_a_stopped_build_with_a_failed_cleanup_never_blocks_a_fresh_build(
    tmp_path, postgres_engine
):
    fresh = stopped_build_releases_its_memory(tmp_path, postgres_engine)

    assert fresh.state in {"queued", "running"}


def test_a_running_build_counts_and_keeps_its_memory_claim(tmp_path, postgres_engine):
    sessions, _builds, operations, _storage, now, node_id, _revision, plan = _services(
        tmp_path, postgres_engine
    )
    job = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    _issue(sessions, job.id)

    _reconcile(sessions, now + OWNER_SETTLE_GRACE)

    assert _counted(sessions, node_id) > 0
    assert _held_kinds(sessions, plan.build_id) == {"disk", "host-memory"}
    # A shortfall it causes names it: the wait is on this build.
    with sessions() as session:
        totals = memory_reservations(session, node_id, memory_pool="separate")
    assert {
        (holder.owner.kind, holder.owner.id)
        for holders in totals.unmaterialized_holders_by_kind.values()
        for holder in holders
    } == {(OwnerKind.RECIPE_BUILD, plan.build_id)}
