"""Recipe routes: rank publication."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from vonk_forge_contracts import (
    EndpointAlias,
)
from vonk_forge_contracts.endpoint_alias import ENDPOINT_ALIAS_MAX_LENGTH

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.outcome import UnknownError
from vonk_agent_protocol.route_activation import SupervisorPhase
from vonk_agent_protocol.state_machines import (
    DesiredAssignmentState,
    EndpointState,
    RouteState,
    RunState,
)
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profile_contract.endpoints import (
    FleetProfileEndpointAssignmentIntent,
    FleetProfileEndpointIntent,
)
from vonk_control.gateway.litellm import LiteLlmGeneration, ServedModelInfo
from vonk_control.gateway.route_runtime import (
    AtomicRouteBundlePublisher,
)
from vonk_control.models.execution import (
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.mapping import ClusterMapping
from vonk_control.models.routes import (
    RecipeRouteAuthority,
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.operation_api.durable import durable_operation_services
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operations.operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.service import RecipeRouteService
from vonk_control.recipe_routes.shared import (
    RecipeRouteNotReady,
    route_publication_owner_lock_statement,
)

from .node_contacts import contact_row
from .observed_actions import observe_action
from .recipe_routes_support import (
    GATEWAY,
    NOW,
    MutableClock,
    _agents_reached_the_controller,
    _aware,
    _live_models,
    _publication,
    _publication_owner,
    _recipe_run,
    add_running_run,
    atomic_service,
    setup,
)
from .test_route_runtime import _inspected


class OverlapPublisher(AtomicRecipeRoutePublisher):
    """Expose crossed candidates without deadlocking a serialized publisher.

    An in-memory stand-in: it never touches a route bundle.
    """

    def __init__(self, generation: int) -> None:
        self._generation = generation
        self._guard = threading.Lock()
        self._second_publish = threading.Event()
        self.aliases: list[tuple[str, ...]] = []

    def served_model_info(self):
        return ServedModelInfo()

    def publish_recipe(self, candidate, *, required=SupervisorPhase.READY):
        return self._record(
            tuple(sorted(candidate.state.aliases)), candidate.state.digest
        )

    def publish_empty(self, route_digest):
        return self._record((), route_digest)

    def _record(self, aliases: tuple[str, ...], digest: str) -> LiteLlmGeneration:
        with self._guard:
            self._generation += 1
            generation = self._generation
            publish_index = len(self.aliases)
            self.aliases.append(aliases)
            if publish_index == 1:
                self._second_publish.set()
        if publish_index == 0:
            # A crossing window: serialized (correct) publication lets it expire.
            self._second_publish.wait(timeout=0.25)  # wall-clock: expected expiry
        return LiteLlmGeneration(generation, digest, digest, "memory")


def test_all_ranks_must_be_fresh_and_ready_but_only_entrypoint_is_routed(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path)
    generation = service.publish_run(run_id)

    document = json.loads(applied[-1])
    assert generation.generation == 1
    assert len(document["model_list"]) == 1
    assert document["model_list"][0]["model_name"] == "qwen"
    assert (
        document["model_list"][0]["litellm_params"]["api_base"]
        == "http://10.0.0.2:8000/v1"
    )
    assert b"10.0.0.3" not in applied[-1]


def test_legacy_distributed_route_is_rejected_without_exact_local_rank_evidence(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, exact_distributed=False)

    observe_action(lambda: service.publish_run(run_id))

    assert applied == []


def test_nonzero_mapping_owner_routes_with_its_accepted_rank_identity(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, endpoint_owner_rank=1)

    with service.sessions() as session:
        candidate = service.candidate_in_session(
            session,
            include_run_id=run_id,
            exclude_run_ids=frozenset(),
            lock=False,
            served=service.served_model_info(),
        )
    endpoint = candidate.endpoints["qwen"]
    assert endpoint.node_id == "spk_" + "2".zfill(32)
    assert endpoint.operation_id == f"recipe:{run_id}:rank:1"
    assert endpoint.api_base == "http://10.0.0.3:8000/v1"

    service.publish_run(run_id)
    model = json.loads(applied[-1])["model_list"][0]
    assert model["litellm_params"]["api_base"] == "http://10.0.0.3:8000/v1"


def test_mapping_owner_must_be_the_run_entrypoint(tmp_path: Path) -> None:
    service, _publisher, applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        mapping = session.get(ClusterMapping, run.mapping_id)
        assert mapping is not None
        worker = session.query(RunNode).filter_by(run_id=run_id, rank=1).one()
        mapping.endpoint_owner_node_id = worker.node_id

    observe_action(lambda: service.publish_run(run_id))
    assert applied == []


def test_public_alias_routes_to_primary_runtime_model_alias(tmp_path: Path) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path,
        run_alias="public-qwen",
        runtime_model_aliases=("internal-qwen", "compat-qwen"),
    )

    service.publish_run(run_id)

    model = json.loads(applied[-1])["model_list"][0]
    assert model["model_name"] == "public-qwen"
    assert model["litellm_params"]["model"] == "openai/internal-qwen"


def test_alias_comes_only_from_published_v1_recipe_run(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path,
        run_alias="agent-alias",
        runtime_model_aliases=("deepseek-v4-flash-dspark",),
    )

    service.publish_run(run_id)

    document = json.loads(applied[-1])
    assert [row["model_name"] for row in document["model_list"]] == ["agent-alias"]
    assert (
        document["model_list"][0]["litellm_params"]["model"]
        == "openai/deepseek-v4-flash-dspark"
    )


def test_artifact_interface_never_publishes_a_litellm_route(tmp_path: Path) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path, interfaces=[{"adapter": "video-job"}]
    )

    observe_action(lambda: service.publish_run(run_id))

    assert applied == []


def test_missing_runtime_model_authority_blocks_route_publication(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, runtime_model_aliases=())

    observe_action(lambda: service.publish_run(run_id))

    assert applied == []


def test_tailnet_endpoint_is_accepted_by_configured_management_policy(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, ranks=1)
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=run_id).one()
        node.endpoint = {"url": "http://100.100.20.30:8000"}
    service._management_policy = ManagementAddressPolicy.parse("100.64.0.0/10")

    service.publish_run(run_id)
    assert b"http://100.100.20.30:8000/v1" in applied[-1]


@pytest.mark.parametrize(("stale", "failed"), [(True, False), (False, True)])
def test_stale_or_failed_rank_blocks_gang_publication(
    tmp_path: Path, stale: bool, failed: bool
) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path, stale=stale, failed_rank=failed
    )
    observe_action(lambda: service.publish_run(run_id))
    assert applied == []


@pytest.mark.usefixtures("damaged_json_rows")
def test_candidate_rank_identity_must_exactly_match_accepted_plan(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        invalid_plan: dict[str, object] = {
            "nodes": [
                {
                    "node_id": "spk_" + "9" * 32,
                    "rank": rank,
                    "role": "entrypoint" if rank == 0 else "worker",
                }
                for rank in range(2)
            ]
        }
        run.plan = invalid_plan

    observe_action(lambda: service.publish_run(run_id))
    assert applied == []


def test_invalid_candidate_retains_previous_generation(tmp_path: Path) -> None:
    service, publisher, _applied, run_id = setup(tmp_path)
    accepted = service.publish_run(run_id)
    rejecting = RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(
            AtomicRouteBundlePublisher(
                tmp_path / "litellm",
                validate_litellm=lambda _: False,
            ),
        ),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=120,
    )
    observe_action(lambda: rejecting.withdraw_run(run_id))
    assert _inspected(publisher).generation == accepted.generation


def test_withdraw_publishes_empty_generation_before_workload_stop(
    tmp_path: Path,
) -> None:
    service, publisher, applied, run_id = setup(tmp_path)
    service.publish_run(run_id)
    empty = service.withdraw_run(run_id)

    assert empty is not None
    assert empty.generation == 2
    assert json.loads(applied[-1])["model_list"] == []
    assert _inspected(publisher).generation == empty.generation


def test_disjoint_sqlite_withdrawals_serialize_one_global_candidate(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, first_run = setup(tmp_path)
    second_run = add_running_run(
        service,
        first_run,
        alias="second",
        route_state="pending",
        identity=3,
    )
    service.publish_run(first_run)
    second_generation = service.publish_run(second_run)
    overlap = OverlapPublisher(second_generation.generation)
    service._publisher = overlap
    start = threading.Barrier(2)

    def withdraw(run_id: str) -> None:
        start.wait()
        service.withdraw_run(run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(withdraw, run_id) for run_id in (first_run, second_run)]
        for future in futures:
            future.result(timeout=30)

    with service.sessions() as session:
        assert [
            _recipe_run(session, run_id).route_state
            for run_id in (first_run, second_run)
        ] == ["withdrawn", "withdrawn"]
    assert overlap.aliases[-1] == ()


def test_route_publication_owner_lock_compiles_for_postgresql() -> None:
    statement_factory = route_publication_owner_lock_statement
    assert callable(statement_factory)

    sql = str(
        statement_factory().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE OF route_publication_owner" in sql


def test_candidate_contains_published_runs_and_explicit_pending_run_only(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, published_run = setup(tmp_path)
    service.publish_run(published_run)
    add_running_run(
        service,
        published_run,
        alias="unpublished",
        route_state="withdrawn",
        identity=3,
    )
    pending_run = add_running_run(
        service,
        published_run,
        alias="candidate",
        route_state="pending",
        identity=4,
    )

    service.publish_run(pending_run)

    assert [model["model_name"] for model in json.loads(applied[-1])["model_list"]] == [
        "candidate",
        "qwen",
    ]


class _PublicationFault:
    """Real route service whose first publication attempts fail transiently.

    ``publish_run`` is the boundary the worker drives, so injecting the failure
    here exercises the worker's own classification, durable retry scheduling
    and the real publication that follows it.
    """

    def __init__(
        self,
        inner: RecipeRouteService,
        error: BaseException,
        *,
        failures: int = 1,
    ) -> None:
        self.sessions = inner.sessions
        self._inner = inner
        self._error = error
        self._remaining = failures
        self.attempts = 0

    def publish_run(self, run_id: str) -> LiteLlmGeneration:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self._inner.publish_run(run_id)

    def maintain(self) -> bool:
        return self._inner.maintain()


def test_temporary_publication_failure_stays_pending_and_converges(
    tmp_path: Path,
) -> None:
    """A brief supervisor failure must not permanently fail a ready route.

    The run keeps ``pending`` with a durable next-attempt time, the dependency
    is not hammered before that time, and restoring it converges without
    another operator command or any runtime start/stop command.
    """

    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
    now = [NOW]
    routes = _PublicationFault(service, OSError("supervisor socket unavailable"))
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: now[0])

    assert worker.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "pending"
        assert run.route_attempts == 1
        assert "supervisor socket unavailable" in (run.route_error or "")
        recorded_due_at = run.next_action_at
    assert recorded_due_at is not None
    due_at = _aware(recorded_due_at)

    # Not due yet, so the worker must not attempt publication again.
    assert worker.tick() is False
    assert routes.attempts == 1

    now[0] = due_at + timedelta(seconds=1)
    assert worker.tick() is True
    assert routes.attempts == 2
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        assert run.route_attempts == 0
        assert run.next_action_at is None
        assert run.route_error is None


def test_publication_recovers_after_more_than_a_request_retry_budget(
    tmp_path: Path,
) -> None:
    # Wrong implementation: an uncertain publication ends after six attempts,
    # leaving the same running intent unpublished after the dependency recovers.
    clock = MutableClock(NOW)
    service, _publisher, _applied, run_id = setup(tmp_path, clock=clock)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.PENDING
    routes = _PublicationFault(
        service, OSError("supervisor socket unavailable"), failures=12
    )
    for attempt in range(13):
        with service.sessions.begin() as session:
            for node in session.query(RunNode).filter_by(run_id=run_id):
                node.updated_at = clock.now
                node.observation_observed_at = clock.now
        # Restart the observer while retaining the same durable run and backoff.
        worker = RecipeOperationWorker(service.sessions, routes, clock=clock)
        worker.tick()
        with service.sessions() as session:
            run = _recipe_run(session, run_id)
            assert run.state == RunState.RUNNING
            if attempt < 12:
                assert run.route_state == RouteState.PENDING
                assert run.next_action_at is not None
                assert clock.now < _aware(run.next_action_at)
                assert _aware(run.next_action_at) <= clock.now + timedelta(seconds=60)
                clock.now = _aware(run.next_action_at) + timedelta(seconds=1)
            else:
                assert run.route_state == RouteState.PUBLISHED
    assert routes.attempts == 13


def test_invalid_publication_ends_immediately_with_its_evidence(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    service, _publisher, _applied, run_id = setup(tmp_path, clock=clock)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.PENDING
    error = RuntimeError("recipe route document is invalid")
    routes = _PublicationFault(service, error, failures=12)
    worker = RecipeOperationWorker(service.sessions, routes, clock=clock)
    worker.tick()
    worker.tick()
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.FAILED
        assert run.next_action_at is None
        assert run.route_error is not None and str(error) in run.route_error
    assert routes.attempts == 1


def test_worker_publishes_pending_route_and_records_failure(tmp_path: Path) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: NOW)

    assert worker.tick() is True
    assert worker.tick() is False
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        assert run.route_generation == 1

    failed_service, _publisher, _applied, failed_run = setup(
        tmp_path / "failed", validate=lambda _: False
    )
    with failed_service.sessions.begin() as session:
        _recipe_run(session, failed_run).route_state = "pending"
    RecipeOperationWorker(
        failed_service.sessions, failed_service, clock=lambda: NOW
    ).tick()
    with failed_service.sessions() as session:
        failed = _recipe_run(session, failed_run)
        assert failed.route_state == "pending"
        assert failed.next_action_at is not None
        assert failed.route_error is not None
        assert "LiteLLM validation" in failed.route_error


def test_not_ready_pending_run_does_not_starve_later_run_or_maintenance(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, first_run = setup(tmp_path)
    second_run = add_running_run(
        service,
        first_run,
        alias="second",
        route_state="pending",
        identity=3,
    )
    with service.sessions.begin() as session:
        _recipe_run(session, first_run).route_state = "pending"

    class Routes(RecipeRouteService):
        def __init__(self) -> None:
            self.ready = {second_run}
            self.published: list[str] = []
            self.maintained = 0

        def publish_run(self, run_id: str) -> LiteLlmGeneration:
            if run_id not in self.ready:
                raise RecipeRouteNotReady("awaiting exact observation")
            self.published.append(run_id)
            return LiteLlmGeneration(
                generation=0,
                route_digest="",
                config_sha256="",
                path="memory",
            )

        def maintain(self) -> bool:
            self.maintained += 1
            return True

    routes = Routes()
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: NOW)
    assert worker.tick() is True
    assert routes.published == [second_run]
    assert routes.maintained == 1

    routes.ready.clear()
    assert worker.tick() is True
    assert routes.maintained == 2


def _sparks_present(service, now) -> None:
    """Every Spark has just reported: late telemetry is then its run's own."""

    with service.sessions.begin() as session:
        for node in session.query(AgentNode):
            contact_row(node).last_seen_at = now


def test_initial_observation_waits_while_its_spark_is_away(tmp_path: Path) -> None:
    """Catches a route withdrawn mid-smoke because its Spark's report was late.

    A Spark that is away owns the late first telemetry: the route keeps
    waiting past the grace; once the Spark is present and still reports
    nothing for its run, a whole grace later the route is withdrawn.
    """
    now = [NOW]
    service, _publisher, _applied, run_id = setup(tmp_path, clock=lambda: now[0])
    deadline = NOW + timedelta(seconds=120)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.route_state = RouteState.PENDING
        run.observation_deadline_at = deadline
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.observed_run_generation = None
            node.observation_observed_at = None
            node.observation_endpoint_ready = None
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: now[0])
    for _ in range(5):  # ten minutes, five old graces, the Spark away
        now[0] += timedelta(seconds=121)
        worker._expire_initial_observation_deadline()
        with service.sessions() as session:
            assert _recipe_run(session, run_id).route_state == RouteState.PENDING
    _sparks_present(service, now[0])
    now[0] += timedelta(seconds=121)
    _sparks_present(service, now[0])
    assert worker._expire_initial_observation_deadline() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == RouteState.WITHDRAWN
        assert run.state == RunState.RUNNING


@pytest.mark.parametrize("route_state", [RouteState.PENDING, RouteState.FAILED])
def test_initial_observation_expiry_preserves_workload_and_allows_fresh_route(
    tmp_path: Path, route_state: RouteState
) -> None:
    """Expiry must not fabricate rank failure or poison the next route request."""
    now = [NOW]
    service, _publisher, applied, run_id = setup(tmp_path, clock=lambda: now[0])
    deadline = NOW + timedelta(seconds=60)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.route_state = route_state
        run.observation_deadline_at = deadline
        run.next_action_at = deadline + timedelta(seconds=30)
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.observed_run_generation = None
            node.observation_observed_at = None
            node.observation_endpoint_ready = None

    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: now[0])
    now[0] = deadline - timedelta(microseconds=1)
    assert worker._expire_initial_observation_deadline() is False
    assert applied == []
    now[0] = deadline
    _sparks_present(service, now[0])
    # Reconstruct the worker: the deadline belongs to persisted state.
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: now[0])
    assert worker.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        assert run.route_state == RouteState.WITHDRAWN
        assert run.route_error == "initial exact observation deadline elapsed"
        assert run.next_action_at is None
        assert run.observation_deadline_at is None
        assert run.state == RunState.RUNNING
        assert all(node.state == RunState.RUNNING for node in nodes)
        assert all(node.observation_observed_at is None for node in nodes)

    fresh = add_running_run(
        service, run_id, alias="qwen", route_state=RouteState.PENDING, identity=9
    )
    assert worker.tick() is True
    with service.sessions() as session:
        assert _recipe_run(session, fresh).route_state == RouteState.PUBLISHED
        assert _recipe_run(session, run_id).route_state == RouteState.WITHDRAWN
    assert _live_models(tmp_path / "litellm") == ["qwen"]


def test_continuing_publication_preserves_initial_observation_lifetime(
    tmp_path: Path,
) -> None:
    """Ongoing publication cannot erase the exact-observation deadline."""
    now = [NOW]
    service, _publisher, _applied, run_id = setup(tmp_path, clock=lambda: now[0])
    deadline = NOW + timedelta(minutes=10)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.route_state = RouteState.PENDING
        run.observation_deadline_at = deadline
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.observed_run_generation = None
            node.observation_observed_at = None
            node.observation_endpoint_ready = None

    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: now[0])
    while now[0] < deadline:
        worker.tick()
        with service.sessions() as session:
            due = _recipe_run(session, run_id).next_action_at
        if due is None:
            break
        now[0] = due.replace(tzinfo=UTC)
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == RouteState.PENDING
        assert run.next_action_at is not None
        assert run.observation_deadline_at is not None
        assert run.observation_deadline_at.replace(tzinfo=UTC) == deadline

    # A pending, never-published observation cannot supply unchecked evidence
    # to a fresh route's bundle, even before the initial deadline expires.
    fresh = add_running_run(
        service, run_id, alias="fresh", route_state=RouteState.PENDING, identity=9
    )
    with service.sessions.begin() as session:
        for node in session.query(RunNode).filter_by(run_id=fresh):
            node.updated_at = now[0]
            node.observation_observed_at = now[0]
    service.publish_run(fresh)
    assert _live_models(tmp_path / "litellm") == ["fresh"]

    now[0] = deadline
    _sparks_present(service, now[0])
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: now[0])
    assert worker.tick() is True
    with service.sessions() as session:
        assert _recipe_run(session, run_id).route_state == RouteState.WITHDRAWN
        assert all(
            node.state == RunState.RUNNING
            for node in session.query(RunNode).filter_by(run_id=run_id)
        )


def test_direct_publication_accepts_renewed_exact_observation_after_initial_deadline(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path)
    deadline = NOW - timedelta(seconds=1)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.plan = {**run.plan, "observation_schema_version": 2}
        run.route_state = "pending"
        run.observation_deadline_at = deadline
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.observed_run_generation = run.run_generation
            node.observation_observed_at = deadline + timedelta(microseconds=1)
            node.observation_endpoint_ready = node.role == "entrypoint" or None
            node.updated_at = deadline + timedelta(microseconds=1)

    # Observation ingress has already enforced the first-observation deadline. The
    # latest timestamp is a renewal and must retain its current health meaning.
    generation = service.publish_run(run_id)
    assert generation.generation == 1


def test_atomic_adapter_keeps_caddy_routes_static_and_activates_litellm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path / "database")
    atomic = AtomicRouteBundlePublisher(
        tmp_path / "live",
    )
    service = RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(atomic),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=120,
    )

    generation = service.publish_run(run_id)
    assert generation.generation == 1
    # Activation names are checksum-bound; use the active marker's exact name.
    marker = json.loads((tmp_path / "live" / "activation.json").read_text())
    directory = tmp_path / "live" / "generations" / marker["directory"]
    routes = json.loads((directory / "routes.json").read_text())
    assert routes["generation"] == generation.generation
    assert routes["state"] == "published"
    from vonk_control.gateway.route_runtime import verify_active_route_bundle

    verified = verify_active_route_bundle(tmp_path / "live")
    assert not isinstance(verified, UnknownError)
    assert verified.routes is not None
    route = verified.routes.routes["qwen"]
    assert route.node_id == "spk_" + "1".zfill(32)
    assert route.port == 8000
    assert route.operation_id == f"recipe:{run_id}:rank:0"
    assert (
        json.loads((directory / "litellm.json").read_text())["model_list"][0][
            "model_name"
        ]
        == "qwen"
    )

    with service.sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None
        assert owner.owner_generation == generation.generation
        publication = session.get(RoutePublication, owner.authority_id)
        authority = session.get(RecipeRouteAuthority, owner.authority_id)
        assert publication is not None and publication.state == "completed"
        assert authority is not None and authority.authority_id == owner.authority_id

    def profile_endpoint_intent(session, number):
        run = session.get(RecipeRun, run_id)
        assert run is not None
        published = run.route_state == "published"
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
                    state=(
                        EndpointState.NOT_PUBLISHED_YET
                        if published
                        else EndpointState.WITHDRAWN
                    ),
                    expected_run_id=run_id if published else None,
                ),
            ),
        )

    projection = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=profile_endpoint_intent,
    )
    assert projection.profile_endpoint is not None
    profile_endpoint = projection.profile_endpoint(3, None, GATEWAY)
    assert profile_endpoint.assignments is not None
    assert profile_endpoint.assignments[0].state == "published"
    assert profile_endpoint.assignments[0].endpoint is not None
    assert profile_endpoint.assignments[0].endpoint.generation == 1
    # Clients use the Controller's inference gateway; the Spark-local
    # serving address is only a diagnostic.
    assert profile_endpoint.assignments[0].endpoint.api_base == GATEWAY
    assert (
        profile_endpoint.assignments[0].endpoint.backend_api_base
        == "http://10.0.0.2:8000/v1"
    )
    with pytest.raises(Exception) as _ending:
        projection.profile_endpoint(3, "other-profile", GATEWAY)

    wrong_owner = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=lambda _session, number: FleetProfileEndpointIntent(
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
                    expected_run_id="00000000-0000-4000-8000-000000000104",
                ),
            ),
        ),
    )
    assert wrong_owner.profile_endpoint is not None
    wrong_owner_endpoint = wrong_owner.profile_endpoint(3, "qwen", GATEWAY)
    assert wrong_owner_endpoint.assignments is not None
    assert wrong_owner_endpoint.assignments[0].state == "withdrawn"
    assert wrong_owner_endpoint.assignments[0].endpoint is None

    from vonk_control.operation_api import durable as operation_projection

    verify_bundle = operation_projection.verify_active_route_bundle
    renewed_during_verification = False
    replacement_generation = None

    def renew_after_bundle_verification(*args, **kwargs):
        nonlocal renewed_during_verification, replacement_generation
        bundle = verify_bundle(*args, **kwargs)
        if not renewed_during_verification:
            renewed_during_verification = True
            with service.sessions.begin() as session:
                owner = session.scalar(
                    select(RunNode).where(RunNode.run_id == run_id, RunNode.rank == 0)
                )
                assert owner is not None
                owner.endpoint = {"url": "http://10.0.0.9:8000"}
            replacement_generation = service.publish_run(run_id).generation
        return bundle

    # The alias and run remain valid, but the verified generation has changed.
    # Checking membership or published state alone would return stale evidence.
    with monkeypatch.context() as patch:
        patch.setattr(
            operation_projection,
            "verify_active_route_bundle",
            renew_after_bundle_verification,
        )
        racing = projection.profile_endpoint(3, "qwen", GATEWAY)
        assert racing.assignments is not None
        assert racing.assignments[0].state == "unavailable"
        assert racing.assignments[0].endpoint is None
    assert renewed_during_verification
    assert replacement_generation is not None
    assert replacement_generation > generation.generation
    current_endpoint = projection.profile_endpoint(3, "qwen", GATEWAY)
    assert current_endpoint.assignments is not None
    assert current_endpoint.assignments[0].state == "published"
    assert current_endpoint.assignments[0].endpoint is not None
    assert current_endpoint.assignments[0].endpoint.generation == replacement_generation

    withdrew_during_verification = False

    def withdraw_after_bundle_verification(*args, **kwargs):
        nonlocal withdrew_during_verification
        bundle = verify_bundle(*args, **kwargs)
        if not withdrew_during_verification:
            withdrew_during_verification = True
            service.withdraw_run(run_id)
        return bundle

    with monkeypatch.context() as patch:
        patch.setattr(
            operation_projection,
            "verify_active_route_bundle",
            withdraw_after_bundle_verification,
        )
        racing = projection.profile_endpoint(3, "qwen", GATEWAY)
        assert racing.assignments is not None
        assert racing.assignments[0].state == "unavailable"
        assert racing.assignments[0].endpoint is None
    assert withdrew_during_verification

    withdrawn = projection.profile_endpoint(3, "qwen", GATEWAY)
    assert withdrawn.assignments is not None
    assert withdrawn.assignments[0].state == "withdrawn"
    assert withdrawn.assignments[0].endpoint is None


def test_longest_profile_alias_is_planned_published_and_discovered(
    tmp_path: Path,
) -> None:
    # Catches a plan, route or endpoint contract that caps aliases shorter
    # than the profile accepts: the profile saves, then the load cannot run.
    alias = "chat-" + "x" * (ENDPOINT_ALIAS_MAX_LENGTH - len("chat-"))
    assert TypeAdapter(EndpointAlias).validate_python(alias) == alias
    service, _publisher, _applied, run_id = setup(
        tmp_path / "database", run_alias=alias
    )
    service = RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(
            AtomicRouteBundlePublisher(tmp_path / "live")
        ),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=120,
    )

    assert service.publish_run(run_id).generation == 1
    marker = json.loads((tmp_path / "live" / "activation.json").read_text())
    litellm = tmp_path / "live" / "generations" / marker["directory"] / "litellm.json"
    assert json.loads(litellm.read_text())["model_list"][0]["model_name"] == alias

    projection = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=lambda _session, number: FleetProfileEndpointIntent(
            number=number,
            profile_id="00000000-0000-4000-8000-000000000101",
            application_id="00000000-0000-4000-8000-000000000102",
            application_state=LifecycleState.SUCCEEDED,
            assignments=(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id="00000000-0000-4000-8000-000000000103",
                    recipe_title="Qwen",
                    desired_state=DesiredAssignmentState.RUNNING,
                    alias=alias,
                    state=EndpointState.NOT_PUBLISHED_YET,
                    expected_run_id=run_id,
                ),
            ),
        ),
    )
    assert projection.profile_endpoint is not None
    found = projection.profile_endpoint(3, alias, GATEWAY)
    assert found.assignments is not None
    assert found.assignments[0].state == "published"
    assert found.assignments[0].endpoint is not None
    assert found.assignments[0].endpoint.alias == alias


def test_worker_republishes_from_fresh_all_rank_evidence_and_recovers_owner(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    first = service.publish_run(run_id)

    clock.now = NOW + timedelta(seconds=240)
    with service.sessions.begin() as session:
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        for node in nodes:
            node.updated_at = clock.now
        session.delete(session.get(RoutePublicationOwner, 1))

    restarted = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert restarted.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert (
            run.route_generation is not None and run.route_generation > first.generation
        )
        assert publication.generation == run.route_generation


def test_fresh_health_timestamp_does_not_churn_route(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    first = service.publish_run(run_id)

    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    for _ in range(3):
        clock.now += timedelta(seconds=100)
        with service.sessions.begin() as session:
            for node in session.query(RunNode).filter_by(run_id=run_id):
                node.updated_at = clock.now
        assert worker.tick() is False
    with service.sessions() as session:
        assert _recipe_run(session, run_id).route_generation == first.generation


def test_worker_republishes_when_the_live_marker_is_unreadable(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    root = tmp_path / "live"
    service = atomic_service(base, root, clock)
    first = service.publish_run(run_id)

    # A marker in a retired format (or otherwise unreadable) must not block
    # publication: the worker replaces it with the next generation.
    activation = root / "activation.json"
    retired = json.loads(activation.read_bytes()) | {"expires_at": NOW.isoformat()}
    activation.write_text(json.dumps(retired))

    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    marker = AtomicRouteBundlePublisher(root).inspect()
    assert not isinstance(marker, UnknownError)
    assert marker.generation == first.generation + 1
    with service.sessions() as session:
        assert _publication_owner(session).owner_generation == marker.generation


def test_a_serving_route_outlives_missing_rank_reports(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing observations are bookkeeping: traffic keeps flowing.

    Wrong implementation: ranks whose reports stopped arriving (here, an
    upgraded agent that could not inspect a run the old agent started) made
    the route stale, and the worker withdrew a model that was still serving.
    """

    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(run_id)

    clock.now = NOW + timedelta(seconds=301)
    _agents_reached_the_controller(service, clock.now)
    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    with caplog.at_level("INFO"):
        worker.tick()
        worker.tick()

    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        owner = _publication_owner(session)
        assert _publication(session, owner.authority_id).state == "completed"
    # It is never silent: the kept route says why, once.
    kept = [
        r for r in caplog.messages if "recipe.route.kept_without_current_evidence" in r
    ]
    assert len(kept) == 1 and "evidence is 301s old" in kept[0]
