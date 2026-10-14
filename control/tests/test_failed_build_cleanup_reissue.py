"""A build cleanup that failed is issued again; it never parks its build.

Fault injection of the production shape: a build cancelled after its order was
issued (attempt > 0) queues one Spark cleanup, and that cleanup fails ("build
effects are not yet confirmed quiescent"). The reconcile issues the next
cleanup under a new request identity; once one succeeds the build Job ends,
its claims are released and a rebuild of the same input is admitted.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import RecipeBuildCode
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import AgentNode, Job
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_builds.common import RecipeBuildUnknown
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_operations.errors import RecipeRetryLater
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_build_cancellation_recovery import _active_claims, _issue
from .test_recipe_builds import RecordingQueue, setup

_CLEANUP = "recipe.build.cleanup.v1"
# Past the bounded retry clock's longest pause, within the builder's inventory
# freshness.
_PAST_RETRY = timedelta(seconds=90)


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _services(tmp_path, engine):
    sessions, bundles, now, node_id, revision = setup(tmp_path, engine=engine)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    clock = _Clock(now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=clock,
        builds=builds,
    )
    plan = builds.plan(revision.id, node_id, now=now)
    return sessions, operations, clock, node_id, plan


def _cleanups(sessions):
    with sessions() as session:
        return [
            (job.id, job.request_id, job.state)
            for job in session.scalars(
                select(Job).where(Job.kind == _CLEANUP).order_by(Job.created_at)
            )
        ]


def _cancel_mid_run(sessions, operations, plan):
    job = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    _issue(sessions, job.id)
    operations.cancel(
        job.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel this preparation",
    )
    return job


def _request_build(operations, plan):
    return operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )


def test_a_failed_cleanup_is_reissued_until_the_build_ends(tmp_path, postgres_engine):
    sessions, operations, clock, node_id, plan = _services(tmp_path, postgres_engine)
    original = _cancel_mid_run(sessions, operations, plan)
    claims = _active_claims(sessions, plan.build_id)
    assert claims
    [(first, first_key, _state)] = _cleanups(sessions)

    operations.record_node_result(
        first,
        node_id,
        succeeded=False,
        evidence={"reason": "build effects are not yet confirmed quiescent"},
    )
    assert _cleanups(sessions)[0][2] == "failed"
    assert operations.get(original.id).state == "running"
    with pytest.raises(RecipeRetryLater) as pending:
        _request_build(operations, plan)
    assert RecipeBuildCode.CANCELLATION_PENDING in str(pending.value)
    assert original.id in str(pending.value)

    # The next cleanup is paced on the bounded retry clock, never abandoned.
    clock.now += _PAST_RETRY
    assert operations.reconcile_cancelled_builds() is True
    issued = _cleanups(sessions)
    assert len(issued) == 2
    second, second_key, second_state = issued[1]
    assert second_key != first_key and second_state == "running"
    # A cleanup in flight is never duplicated.
    clock.now += _PAST_RETRY
    assert operations.reconcile_cancelled_builds() is False
    assert len(_cleanups(sessions)) == 2
    assert _active_claims(sessions, plan.build_id) == claims

    operations.record_node_result(second, node_id, succeeded=True, evidence={})

    assert operations.get(original.id).state == "cancelled"
    assert not _active_claims(sessions, plan.build_id)
    assert operations.reconcile_cancelled_builds() is False
    fresh = _request_build(operations, plan)
    assert fresh.id != original.id and fresh.owner_id == original.owner_id
    assert fresh.state in {"queued", "running"}
    assert _active_claims(sessions, plan.build_id)


def test_a_retired_builder_ends_the_cancelled_build(tmp_path, postgres_engine):
    sessions, operations, clock, node_id, plan = _services(tmp_path, postgres_engine)
    original = _cancel_mid_run(sessions, operations, plan)
    [(first, _key, _state)] = _cleanups(sessions)
    operations.record_node_result(first, node_id, succeeded=False, evidence={})
    clock.now += _PAST_RETRY
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.revoked_at = clock.now

    # A retired Spark can never run a cleanup: nothing of the build is left
    # on the fleet, so the build ends instead of waiting for one.
    assert operations.reconcile_cancelled_builds() is True

    assert len(_cleanups(sessions)) == 1
    assert operations.get(original.id).state == "cancelled"
    assert not _active_claims(sessions, plan.build_id)
    # Only the retired Spark still refuses a rebuild there, not the cancel.
    with pytest.raises(RecipeBuildUnknown) as refused:
        _request_build(operations, plan)
    assert RecipeBuildCode.CANCELLATION_PENDING not in str(refused.value)
