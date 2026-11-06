"""An installation never stays in flight once nothing owns it.

The install order of an ``installing`` installation can end without an agent
result: a newer workload intent supersedes it, or a person cancels it, before
any Spark claimed it. The installation must then leave ``installing`` by itself
and reach removal through the existing leftover path.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import InstallationState
from vonk_control.attempt_residues import LEFTOVER_GRACE, AttemptResidueReconciler
from vonk_control.installation_transitions import end_orphaned_installation
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
)
from vonk_control.owner_liveness import OWNER_SETTLE_GRACE
from vonk_control.reservation_owners import ADOPTION_WINDOW

from .test_attempt_residues import _leave_incomplete, _load, _reconciler, _uninstalls
from .test_recipe_operations import setup_services

SETTLED = OWNER_SETTLE_GRACE + timedelta(minutes=1)
PAST_GRACE = SETTLED + LEFTOVER_GRACE + timedelta(minutes=1)


def _state(sessions, installation_id: str) -> str:
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        return str(installation.state)


# -- the sweep step -----------------------------------------------------------


def test_an_installing_row_no_operation_names_becomes_partial_then_is_uninstalled(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    installation_id = _leave_incomplete(
        load, state="installing", rank_state="planned", installed_bytes=0
    )
    # Within the settle grace a reader may still be racing the owner's commit.
    assert not _reconciler(load, after=timedelta(minutes=1)).tick()
    assert _state(load.sessions, installation_id) == "installing"
    assert _reconciler(load, after=SETTLED).tick()
    assert _state(load.sessions, installation_id) == "partial"
    assert not _uninstalls(load, installation_id)
    _reconciler(load, after=PAST_GRACE).tick()
    assert len(_uninstalls(load, installation_id)) == 1


def test_a_relaunch_that_rewrote_the_row_wins_the_compare_and_set(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    installation_id = _leave_incomplete(
        load, state="installing", rank_state="planned", installed_bytes=0
    )
    with load.sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        seen = installation.updated_at
        installation.updated_at = seen + timedelta(minutes=1)  # the re-launch
    with load.sessions.begin() as session:
        assert not end_orphaned_installation(
            session, installation_id, seen, load.lifecycle._clock()
        )
    assert _state(load.sessions, installation_id) == "installing"


# -- every state with no live owner reaches removal ---------------------------

# A healthy INSTALLED row is kept on purpose; the unused-storage collector
# removes it by retention, which test_unused_storage_collection covers.
_EXEMPT = {InstallationState.UNINSTALLED, InstallationState.INSTALLED}


@pytest.mark.parametrize(
    "state", [s for s in InstallationState if s not in _EXEMPT], ids=lambda s: s.value
)
def test_every_ownerless_installation_state_reaches_removal(
    tmp_path: Path, state: InstallationState
) -> None:
    load = _load(tmp_path)
    in_flight = state in {InstallationState.PLANNED, InstallationState.INSTALLING}
    installation_id = _leave_incomplete(
        load,
        state=state.value,
        rank_state="planned" if in_flight else "failed",
        installed_bytes=0 if in_flight else 60,
    )
    _reconciler(load, after=SETTLED).tick()
    _reconciler(load, after=PAST_GRACE + ADOPTION_WINDOW + timedelta(hours=1)).tick()
    assert (
        _state(load.sessions, installation_id) == "uninstalled"
        or len(_uninstalls(load, installation_id)) == 1
    )


# -- the two endings that skip the installation -------------------------------


def _setup(tmp_path: Path):
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    return sessions, service, mapping_id, build_id


def _queued_install(tmp_path: Path):
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    assert _state(sessions, operation.owner_id) == "installing"
    return sessions, service, operation, nodes


def _residues(sessions, service, after: timedelta) -> AttemptResidueReconciler:
    return AttemptResidueReconciler(
        sessions,
        abandon_never_installed=service.abandon_never_installed,
        removal=service,
        clock=lambda: service._clock() + after,
    )


def _supersede(sessions, service, operation) -> None:
    with sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            node.workload_intent_ordinal = 4
    assert service.reconcile_superseded_unissued(
        "recipe.install", operation.owner_id, 4
    )


def test_an_installing_row_its_live_install_names_is_left_alone(
    tmp_path: Path,
) -> None:
    sessions, service, operation, _nodes = _queued_install(tmp_path)
    _residues(sessions, service, PAST_GRACE).tick()
    assert _state(sessions, operation.owner_id) == "installing"


def _cancel(sessions, service, operation) -> None:
    service.cancel(
        operation.id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        reason="a person cancelled it",
    )


@pytest.mark.parametrize("ending", [_supersede, _cancel], ids=["supersede", "cancel"])
def test_an_install_ended_before_any_claim_is_removed_by_the_sweep(
    tmp_path: Path, ending
) -> None:
    sessions, service, operation, nodes = _queued_install(tmp_path)
    ending(sessions, service, operation)
    with sessions() as session:
        ended = session.get(Job, operation.id)
        assert ended is not None and ended.state == "cancelled"
    owner = operation.owner_id

    _residues(sessions, service, SETTLED).tick()
    assert _state(sessions, owner) == "partial"
    _residues(sessions, service, PAST_GRACE).tick()
    with sessions() as session:
        uninstall = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
    assert uninstall is not None
    service.record_node_result(uninstall.id, nodes[0], succeeded=True, evidence={})
    assert _state(sessions, owner) == "uninstalled"


def _cancelled_install(tmp_path: Path):
    sessions, service, operation, nodes = _queued_install(tmp_path)
    _cancel(sessions, service, operation)
    return sessions, service, operation, nodes


@pytest.mark.parametrize("ending", [_supersede, _cancel], ids=["supersede", "cancel"])
def test_a_fresh_install_after_the_ending_is_not_blocked(
    tmp_path: Path, ending
) -> None:
    sessions, service, mapping_id, build_id = _setup(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    ending(sessions, service, operation)
    plan = service.preview_install(mapping_id, build_id)
    fresh = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    assert fresh.state in {"queued", "running"}
    assert _state(sessions, fresh.owner_id) == "installing"


def test_removal_is_allowed_once_the_recipe_revision_is_no_longer_active(
    tmp_path: Path,
) -> None:
    sessions, service, operation, _nodes = _cancelled_install(tmp_path)
    owner = operation.owner_id
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, owner)
        assert installation is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == installation.recipe_revision_id)
            .values(state="failed")  # no longer active
        )
    _residues(sessions, service, SETTLED).tick()
    assert _state(sessions, owner) == "partial"
    _residues(sessions, service, PAST_GRACE).tick()
    with sessions() as session:
        queued = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert queued is not None


def test_a_failed_uninstall_of_a_never_written_rank_leaves_a_collectable_row(
    tmp_path: Path,
) -> None:
    sessions, service, operation, nodes = _cancelled_install(tmp_path)
    owner = operation.owner_id
    _residues(sessions, service, SETTLED).tick()
    _residues(sessions, service, PAST_GRACE).tick()
    with sessions() as session:
        uninstall = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
    assert uninstall is not None
    service.record_node_result(uninstall.id, nodes[0], succeeded=False, evidence={})
    assert _state(sessions, owner) == "failed"  # the unused-storage collector's
