from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    DesiredAssignmentState,
    EndpointState,
    LifecycleState,
    UnknownError,
    WaitReason,
)
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profile_contract import (
    FleetProfileEndpointAssignmentIntent,
    FleetProfileEndpointIntent,
)
from vonk_control.gateway.litellm import ServedModelInfo
from vonk_control.gateway.route_runtime import (
    AtomicRouteBundlePublisher,
)
from vonk_control.jobs.documents import RecipeStartParent
from vonk_control.models import (
    AgentNode,
    Job,
    RunNode,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import (
    AtomicRecipeRoutePublisher,
    RecipeRouteNotReady,
    RecipeRouteOwnerBusy,
    RecipeRouteService,
)

from .recipe_routes_support import (
    GATEWAY,
    NOW,
    MutableClock,
    _aware,
    _live_models,
    _publication,
    _publication_owner,
    _recipe_run,
    add_running_run,
    atomic_service,
    setup,
)


@pytest.mark.parametrize(
    ("failure_point", "recovering"),
    [
        ("before-activation", False),
        ("after-activation", False),
        ("after-activation", True),
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_publication_recovers_after_worker_restart_without_new_effect(
    tmp_path: Path, postgres_engine, failure_point: str, recovering: bool
) -> None:
    """Unavailable acknowledgement stays pending; restart adopts exact bytes."""

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(
        tmp_path / "database", clock=clock, engine=postgres_engine
    )
    root = tmp_path / "live"
    acknowledgements: list[int] = []
    fail_ack = [failure_point == "after-activation"]

    def acknowledge(marker, _required):
        acknowledgements.append(marker.generation)
        if fail_ack[0]:
            raise OSError("supervisor acknowledgement lost")

    runtime = AtomicRouteBundlePublisher(root, await_supervisor_ack=acknowledge)
    if failure_point == "before-activation":
        activate = runtime._activate
        fail_activate = [True]

        def activate_once(**kwargs):
            if fail_activate[0]:
                raise OSError("supervisor unavailable before activation")
            return activate(**kwargs)

        runtime._activate = activate_once
    service = RecipeRouteService(
        base.sessions,
        publisher=AtomicRecipeRoutePublisher(runtime),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
        if recovering:
            session.add(
                Job(
                    request_id=str(uuid4()),
                    kind="recipe.start",
                    state="succeeded",
                    actor="system:distributed-recovery",
                    authority_revision="a" * 64,
                    targets=[],
                    payload_digest="b" * 64,
                    payload={
                        "owner_id": run_id,
                        "recovery": {
                            "schema_version": 1,
                            "failed_rank": 1,
                            "deadline": (NOW + timedelta(seconds=90)).isoformat(),
                        },
                    },
                    result={},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    marker_after_failure = (
        AtomicRouteBundlePublisher(root).inspect()
        if failure_point == "after-activation"
        else None
    )
    assert marker_after_failure is None or not isinstance(
        marker_after_failure, UnknownError
    )
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.state == "running"
        assert run.route_state == "pending"
        assert run.next_action_at is not None
        due_at = _aware(run.next_action_at)
        if recovering:
            recovery_job = session.query(Job).filter_by(kind="recipe.start").one()
            assert recovery_job.result == {}

    clock.now = due_at + timedelta(seconds=1)
    fail_ack[0] = False
    if failure_point == "before-activation":
        fail_activate[0] = False
    restarted_runtime = AtomicRouteBundlePublisher(
        root, await_supervisor_ack=acknowledge
    )
    restarted_service = RecipeRouteService(
        base.sessions,
        publisher=AtomicRecipeRoutePublisher(restarted_runtime),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )
    assert (
        RecipeOperationWorker(base.sessions, restarted_service, clock=clock).tick()
        is True
    )
    final_marker = AtomicRouteBundlePublisher(root).inspect()
    assert not isinstance(final_marker, UnknownError)
    with base.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        assert run.route_generation == final_marker.generation
        assert run.route_error is None
        if recovering:
            recovery_job = session.query(Job).filter_by(kind="recipe.start").one()
            result = recovery_job.result
            assert result is not None
            assert result["recovery_route_published"] is True
    if marker_after_failure is not None:
        assert final_marker.digest == marker_after_failure.digest
        assert acknowledgements[:-1]
        assert set(acknowledgements) == {final_marker.generation}
    else:
        assert final_marker.generation == 1
        assert acknowledgements == [1]


@pytest.mark.parametrize("run_state", ["lost", "stopping", "running"])
def test_last_route_withdrawn_outside_publication_empties_litellm(
    tmp_path: Path, run_state: str
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(run_id)
    assert _live_models(tmp_path / "live") == ["qwen"]

    # Lifecycle paths (lost agent, cancelled start, observation deadline) mark
    # the route withdrawn in the database without publishing a new bundle.
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.state = run_state
        run.route_state = "withdrawn"

    clock.now += timedelta(seconds=1)
    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    assert _live_models(tmp_path / "live") == []
    with service.sessions() as session:
        owner = _publication_owner(session)
        assert _publication(session, owner.authority_id).state == "routes-withdrawn"
    clock.now += timedelta(seconds=1)
    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is False


def test_invalid_endpoint_bookkeeping_keeps_serving_routes_and_admits_fresh(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, healthy_run = setup(tmp_path / "database", clock=clock)
    broken_run = add_running_run(
        base, healthy_run, alias="broken", route_state="pending", identity=3
    )
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(healthy_run)
    service.publish_run(broken_run)
    assert sorted(_live_models(tmp_path / "live")) == ["broken", "qwen"]
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=broken_run).one()
        node.endpoint = {"url": "http://192.0.2.9:8000"}

    RecipeOperationWorker(service.sessions, service, clock=clock).tick()
    assert sorted(_live_models(tmp_path / "live")) == ["broken", "qwen"]
    with service.sessions() as session:
        assert _recipe_run(session, healthy_run).route_state == "published"
        assert _recipe_run(session, broken_run).route_state == "published"
    fresh = add_running_run(
        base, healthy_run, alias="fresh", route_state="pending", identity=4
    )
    service.publish_run(fresh)
    assert sorted(_live_models(tmp_path / "live")) == ["broken", "fresh", "qwen"]


def test_invalid_model_alias_leaves_out_only_its_route(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, healthy_run = setup(tmp_path / "database", clock=clock)
    bad_run = add_running_run(
        base, healthy_run, alias="bad", route_state="pending", identity=3
    )
    with base.sessions.begin() as session:
        run = _recipe_run(session, bad_run)
        run.plan = {**run.plan, "upstream_model": "/models/Step-3.7-Flash-NVFP4"}
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(healthy_run)

    with pytest.raises(RecipeRouteNotReady, match="route.invalid-alias"):
        service.publish_run(bad_run)
    assert _live_models(tmp_path / "live") == ["qwen"]

    # A pending invalid route never blocks another run's publication.
    fresh = add_running_run(
        base, healthy_run, alias="fresh", route_state="pending", identity=4
    )
    service.publish_run(fresh)
    assert sorted(_live_models(tmp_path / "live")) == ["fresh", "qwen"]


@pytest.mark.parametrize("damage", ["plan", "alias", "ranks", "mapping", "endpoint"])
@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("provider", ["openai", "hosted_vllm"])
def test_bookkeeping_damage_retains_the_accepted_serving_route(
    tmp_path: Path, damage: str, provider: str
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.plan = {**run.plan, "upstream_provider": provider}
    service.publish_run(run_id)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        if damage == "plan":
            run.plan = {"unreadable": True}
        elif damage == "alias":
            run.alias = "invalid alias"
        elif damage == "ranks":
            node.rank = 2
        elif damage == "mapping":
            run.mapping_generation += 1
        else:
            node.endpoint = {"url": "not an endpoint"}
    RecipeOperationWorker(service.sessions, service, clock=clock).tick()
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.state == "running"
        assert run.route_state == "published"
    root = tmp_path / "live"
    marker = json.loads((root / "activation.json").read_text())
    config = json.loads(
        (root / "generations" / marker["directory"] / "litellm.json").read_text()
    )
    assert config["model_list"][0]["litellm_params"]["model"].startswith(provider + "/")


@pytest.mark.parametrize("damage", ["plan", "alias", "ranks", "mapping", "endpoint"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_corrupt_serving_run_does_not_block_other_route_changes_and_recovers(
    tmp_path: Path, damage: str
) -> None:
    from copy import deepcopy

    from vonk_control.gateway.route_runtime import verify_active_route_bundle

    clock = MutableClock(NOW)
    base, _publisher, _applied, first = setup(tmp_path / "database", clock=clock)
    second = add_running_run(
        base, first, alias="second", route_state="pending", identity=3
    )
    root = tmp_path / "live"
    service = atomic_service(base, root, clock)
    service.publish_run(first)
    accepted = verify_active_route_bundle(root)
    assert not isinstance(accepted, UnknownError)
    assert accepted.routes is not None
    assert accepted.litellm is not None
    original_endpoint = deepcopy(accepted.routes.routes["qwen"])
    original_model = deepcopy(accepted.litellm.model_list[0])
    with service.sessions.begin() as session:
        run = _recipe_run(session, first)
        node = session.scalar(select(RunNode).where(RunNode.run_id == first))
        assert node is not None
        original_plan, original_alias = deepcopy(run.plan), run.alias
        original_rank, original_mapping, original_node_endpoint = (
            node.rank,
            run.mapping_generation,
            deepcopy(node.endpoint),
        )
        if damage == "plan":
            run.plan = {"unreadable": True}
        elif damage == "alias":
            run.alias = "invalid alias"
        elif damage == "ranks":
            node.rank = 2
        elif damage == "mapping":
            run.mapping_generation += 1
        else:
            node.endpoint = {"url": "not an endpoint"}
    service.publish_run(second)
    added = verify_active_route_bundle(root)
    assert not isinstance(added, UnknownError)
    assert added.routes is not None
    assert added.litellm is not None
    assert added.routes.routes["qwen"] == original_endpoint
    assert sorted(_live_models(root)) == ["qwen", "second"]
    assert (
        next(model for model in added.litellm.model_list if model.model_name == "qwen")
        == original_model
    )

    def accepted_intent(session, number):
        return FleetProfileEndpointIntent(
            number=number,
            profile_id="00000000-0000-4000-8000-000000000101",
            application_id="00000000-0000-4000-8000-000000000102",
            application_state=LifecycleState.SUCCEEDED,
            assignments=(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id="00000000-0000-4000-8000-000000000103",
                    recipe_title="Qwen",
                    desired_state=DesiredAssignmentState.RUNNING,
                    alias="qwen",
                    state=EndpointState.NOT_PUBLISHED_YET,
                    expected_run_id=first,
                ),
            ),
        )

    projection = durable_operation_services(
        service.sessions,
        root,
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=accepted_intent,
    )
    assert projection.profile_endpoint is not None
    view = projection.profile_endpoint(3, "qwen", GATEWAY)
    assert view.assignments is not None
    assert view.assignments[0].state == EndpointState.PUBLISHED
    assert view.assignments[0].endpoint is not None
    assert view.assignments[0].endpoint.generation == added.marker.generation
    assert view.assignments[0].endpoint.backend_api_base == "http://10.0.0.2:8000/v1"
    service.withdraw_run(second)
    removed = verify_active_route_bundle(root)
    assert not isinstance(removed, UnknownError)
    assert _live_models(root) == ["qwen"]
    assert removed.routes is not None
    assert removed.routes.routes["qwen"] == original_endpoint
    assert service.maintain() is False  # exact snapshot identity does not churn
    with service.sessions.begin() as session:
        run = _recipe_run(session, first)
        node = session.scalar(select(RunNode).where(RunNode.run_id == first))
        assert node is not None
        run.plan, run.alias, run.mapping_generation = (
            original_plan,
            original_alias,
            original_mapping,
        )
        node.rank, node.endpoint = original_rank, original_node_endpoint
        node.updated_at = NOW + timedelta(seconds=1)
    clock.now = NOW + timedelta(seconds=1)
    assert service.maintain() is True
    recovered = verify_active_route_bundle(root)
    assert not isinstance(recovered, UnknownError)
    assert recovered.marker.generation > removed.marker.generation
    assert _live_models(root) == ["qwen"]
    assert recovered.routes is not None
    assert recovered.routes.routes["qwen"].observed_at != original_endpoint.observed_at
    fresh = add_running_run(
        base, first, alias="fresh", route_state="pending", identity=4
    )
    service.publish_run(fresh)
    assert sorted(_live_models(root)) == ["fresh", "qwen"]


@pytest.mark.parametrize("fault", ["stopped", "revoked"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_stopped_rank_is_not_hidden_by_a_corrupt_serving_plan(
    tmp_path: Path, fault: str
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, first = setup(tmp_path / "database", clock=clock)
    second = add_running_run(
        base, first, alias="second", route_state="pending", identity=3
    )
    root = tmp_path / "live"
    service = atomic_service(base, root, clock)
    service.publish_run(first)
    service.publish_run(second)
    with service.sessions.begin() as session:
        _recipe_run(session, first).plan = {"unreadable": True}
        node = session.query(RunNode).filter_by(run_id=first, rank=1).one()
        if fault == "stopped":
            node.state = "stopped"
        else:
            agent = session.get(AgentNode, node.node_id)
            assert agent is not None
            agent.revoked_at = NOW
    assert service.maintain() is True
    assert _live_models(root) == ["second"]
    with service.sessions() as session:
        assert _recipe_run(session, first).route_state == "withdrawn"
        assert _recipe_run(session, second).route_state == "published"
    fresh = add_running_run(
        base, second, alias="fresh", route_state="pending", identity=4
    )
    service.publish_run(fresh)
    assert sorted(_live_models(root)) == ["fresh", "second"]


@pytest.mark.usefixtures("damaged_json_rows")
def test_unverified_active_bundle_cannot_supply_a_fallback_endpoint(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, first = setup(tmp_path / "database", clock=clock)
    second = add_running_run(
        base, first, alias="second", route_state="pending", identity=3
    )
    root = tmp_path / "live"
    service = atomic_service(base, root, clock)
    service.publish_run(first)
    marker = json.loads((root / "activation.json").read_text())
    route_file = root / "generations" / marker["directory"] / "routes.json"
    route_file.write_text(route_file.read_text() + " ")
    with service.sessions.begin() as session:
        _recipe_run(session, first).plan = {"unreadable": True}
    service.publish_run(second)
    assert _live_models(root) == ["second"]
    with service.sessions() as session:
        assert _recipe_run(session, first).plan == {"unreadable": True}
    service.withdraw_run(first)
    assert _live_models(root) == ["second"]


@pytest.mark.parametrize("explicit_null", [False, True])
def test_normal_start_optional_recovery_does_not_block_route_publication(
    tmp_path: Path, explicit_null: bool
) -> None:
    """Catches treating an omitted or null optional marker as a recovery owner."""
    clock = MutableClock(NOW)
    base, _, _, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        payload = RecipeStartParent(
            schema_version=1,
            owner_kind="run",
            owner_id=run_id,
            plan_digest=run.plan_digest,
        )
        session.add(
            Job(
                request_id=str(uuid4()),
                kind=AgentOperation.RECIPE_START,
                state=LifecycleState.SUCCEEDED,
                actor="admin",
                authority_revision="a" * 64,
                targets=[],
                payload_digest="b" * 64,
                payload=payload.model_dump(mode="json", exclude_none=not explicit_null),
                result={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    first = service.publish_run(run_id)
    second = add_running_run(
        base, run_id, alias="next", route_state="pending", identity=3
    )
    service.publish_run(second)
    with service.sessions() as session:
        generation = _recipe_run(session, run_id).route_generation
        assert generation is not None and generation >= first.generation
        assert _recipe_run(session, second).route_state == "published"


@pytest.mark.parametrize("contention", ["owner", "run"])
def test_maintenance_contention_uses_the_typed_wait_schedule(
    tmp_path,
    monkeypatch,
    caplog,
    contention,
) -> None:
    """Run contention must not fall through _advance's generic traceback handler."""
    from vonk_control.recipe_routes.shared import _RecipeRunContended

    clock = MutableClock(NOW)
    routes, _, _, _ = setup(tmp_path, clock=clock)
    worker = RecipeOperationWorker(routes.sessions, routes, clock=clock)
    calls = []

    def maintain():
        calls.append(clock.now)
        if len(calls) == 1:
            raise (
                RecipeRouteOwnerBusy("owner held")
                if contention == "owner"
                else _RecipeRunContended("run changed")
            )
        return True

    monkeypatch.setattr(routes, "maintain", maintain)
    caplog.set_level(logging.INFO, logger="vonk-control-worker")
    assert worker._advance(worker._maintain_routes) is False
    assert not worker._retry_at
    assert worker._route_owner_due is not None
    assert worker._route_owner_due > clock.now
    busy = [
        json.loads(record.getMessage())
        for record in caplog.records
        if "recipe.route.owner_busy" in record.getMessage()
    ]
    assert [wait["reason"] for wait in busy] == [
        WaitReason.OBSERVATION_UNAVAILABLE
        if contention == "owner"
        else WaitReason.SCOPE_CHANGED
    ]
    assert not any(record.exc_info for record in caplog.records)
    assert worker._advance(worker._maintain_routes) is False
    assert len(calls) == 1
    clock.now = worker._route_owner_due
    assert worker._advance(worker._maintain_routes) is True
    assert len(calls) == 2
    assert worker._route_owner_due is None


def _count_maintenance_decisions(service, monkeypatch) -> list[int]:
    calls = [0]

    def not_ready(_session: Session, _served: ServedModelInfo) -> object:
        calls[0] += 1
        raise RecipeRouteNotReady("evidence is not there yet")

    monkeypatch.setattr(service, "_maintenance_step_in_session", not_ready)
    return calls


def test_route_maintenance_decides_once_per_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches in-request retries nested inside the background owner's pass.

    One pass decides once; the durable owner backoff is the only retry.
    """
    service, _publisher, _applied, _run_id = setup(tmp_path)
    calls = _count_maintenance_decisions(service, monkeypatch)
    assert service.maintain() is False
    assert calls[0] == 1
    with service.sessions() as session:
        assert _publication_owner(session).next_action_at is not None


def test_withdrawal_maintenance_raises_after_one_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.recipe_routes import withdrawal as withdrawal_steps

    service, _publisher, _applied, _run_id = setup(tmp_path)
    calls = _count_maintenance_decisions(service, monkeypatch)
    with pytest.raises(RecipeRouteNotReady):
        withdrawal_steps.maintain(service)
    assert calls[0] == 1


def test_maintenance_decides_again_after_the_owner_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one retry path: wait out the durable backoff, then decide again."""
    now = [NOW]
    service, _publisher, _applied, _run_id = setup(tmp_path, clock=lambda: now[0])
    real = service._maintenance_step_in_session
    calls = [0]

    def once_not_ready(session: Session, served: ServedModelInfo) -> object:
        calls[0] += 1
        if calls[0] == 1:
            raise RecipeRouteNotReady("evidence is not there yet")
        return real(session, served)

    monkeypatch.setattr(service, "_maintenance_step_in_session", once_not_ready)
    assert service.maintain() is False
    with service.sessions() as session:
        due = _publication_owner(session).next_action_at
    assert due is not None
    assert service.maintain() is False  # still inside the backoff
    assert calls[0] == 1
    now[0] = _aware(due) + timedelta(seconds=1)
    service.maintain()
    assert calls[0] == 2
    with service.sessions() as session:
        owner = _publication_owner(session)
        assert owner.next_action_at is None  # the pass converged, no new wait
        assert owner.reconciliation_attempts == 0


def test_maintain_does_not_retry_a_failed_pass_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _publisher, _applied, _run_id = setup(tmp_path)
    calls = [0]

    def broken() -> bool:
        calls[0] += 1
        raise OSError("observation store unavailable")

    monkeypatch.setattr(service, "_maintain_once", broken)
    assert service.maintain() is False
    assert calls[0] == 1


def test_publish_run_claims_and_publishes_once_per_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches in-request retries inside the worker's publication pass."""
    service, _publisher, _applied, run_id = setup(tmp_path)
    calls = [0]

    def not_ready(_run_id: str) -> object:
        calls[0] += 1
        raise RecipeRouteNotReady("evidence is not there yet")

    monkeypatch.setattr(service, "_claim_run", not_ready)
    with pytest.raises(RecipeRouteNotReady):
        service.publish_run(run_id)
    assert calls[0] == 1
