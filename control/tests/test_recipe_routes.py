from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from vonk_control import recipe_routes
from vonk_control.auth import TokenCodec
from vonk_control.litellm import LiteLlmPolicyError, LiteLlmPublisher
from vonk_control.models import (
    AgentNode,
    Base,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import (
    AtomicRecipeRoutePublisher,
    RecipeRouteError,
    RecipeRouteService,
)
from vonk_control.route_runtime import AtomicRouteBundlePublisher

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class OverlapPublisher:
    """Expose crossed candidates without deadlocking a serialized publisher."""

    def __init__(self, generation: int) -> None:
        self._generation = generation
        self._guard = threading.Lock()
        self._second_publish = threading.Event()
        self.aliases: list[tuple[str, ...]] = []

    def publish(self, state, _policy):
        with self._guard:
            self._generation += 1
            generation = self._generation
            publish_index = len(self.aliases)
            self.aliases.append(tuple(sorted(state.aliases)))
            if publish_index == 1:
                self._second_publish.set()
        if publish_index == 0:
            self._second_publish.wait(timeout=0.25)
        return type(
            "Generation",
            (),
            {
                "generation": generation,
                "route_digest": state.digest,
                "config_sha256": state.digest,
                "path": "memory",
            },
        )()

    def publish_empty(self, route_digest):
        return self.publish(
            type("State", (), {"aliases": {}, "digest": route_digest})(), None
        )


def setup(
    tmp_path: Path,
    *,
    ranks=2,
    stale=False,
    failed_rank=False,
    validate=lambda _: True,
    clock=None,
    run_alias="qwen",
    runtime_model_aliases=("qwen",),
    interfaces=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    nodes = tuple("spk_" + f"{index + 1:032x}" for index in range(ranks))
    with sessions.begin() as session:
        session.add_all(
            AgentNode(
                node_id=node,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
            )
            for node in nodes
        )
        recipe = LocalRecipe(
            slug="qwen",
            title="Qwen",
            description="Qwen",
            source_kind="local",
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(recipe)
        session.flush()
        revision = LocalRecipeRevision(
            recipe_id=recipe.id,
            revision_number=1,
            lifecycle="resolved",
            schema_version=1,
            document={
                "interfaces": interfaces
                if interfaces is not None
                else [
                    {
                        "adapter": "openai",
                        "model_aliases": list(runtime_model_aliases),
                    }
                ]
            },
            content_sha256="a" * 64,
            created_by="admin",
            created_at=NOW,
        )
        session.add(revision)
        session.flush()
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name=f"{ranks}-node",
            generation=1,
            node_count=ranks,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=nodes[0],
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node,
                rank=rank,
                role="entrypoint" if rank == 0 else "worker",
                endpoint_owner=rank == 0,
                created_at=NOW,
            )
            for rank, node in enumerate(nodes)
        )
        build = RecipeBuild(
            recipe_revision_id=revision.id,
            builder_node_id=nodes[0],
            source_bundle_sha256="e" * 64,
            build_input_sha256="f" * 64,
            state="succeeded",
            policy_report={"passed": True},
            plan={},
            image_digest="sha256:" + "9" * 64,
            oci_layout_sha256="8" * 64,
            image_bytes=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.flush()
        installation = RecipeInstallation(
            recipe_revision_id=revision.id,
            mapping_id=mapping.id,
            mapping_generation=1,
            recipe_build_id=build.id,
            image_digest=build.image_digest,
            plan_digest="b" * 64,
            plan={},
            state="installed",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(installation)
        session.flush()
        session.add_all(
            InstallationNode(
                installation_id=installation.id,
                node_id=node,
                rank=rank,
                role="entrypoint" if rank == 0 else "worker",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=NOW,
            )
            for rank, node in enumerate(nodes)
        )
        run = RecipeRun(
            installation_id=installation.id,
            mapping_id=mapping.id,
            mapping_generation=1,
            alias=run_alias,
            plan_digest="c" * 64,
            plan={},
            state="running",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(run)
        session.flush()
        for rank, node in enumerate(nodes):
            session.add(
                RunNode(
                    run_id=run.id,
                    node_id=node,
                    rank=rank,
                    role="entrypoint" if rank == 0 else "worker",
                    state="failed" if failed_rank and rank == ranks - 1 else "running",
                    port=8000,
                    reserved_memory_bytes=100,
                    endpoint={"url": f"http://10.0.0.{rank + 2}:8000"},
                    evidence_digest=str(rank + 1) * 64,
                    updated_at=NOW - (timedelta(seconds=301) if stale else timedelta()),
                )
            )
    applied: list[bytes] = []
    publisher = LiteLlmPublisher(
        tmp_path / "litellm", validate=validate, apply=applied.append
    )
    service = RecipeRouteService(
        sessions,
        publisher=publisher,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock or (lambda: NOW),
        maximum_age_seconds=300,
    )
    return service, publisher, applied, run.id


def add_running_run(
    service: RecipeRouteService,
    source_run_id: str,
    *,
    alias: str,
    route_state: str,
    identity: int,
) -> str:
    node_id = "spk_" + f"{identity:032x}"
    with service.sessions.begin() as session:
        source = session.get(RecipeRun, source_run_id)
        assert source is not None
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
            )
        )
        run = RecipeRun(
            installation_id=source.installation_id,
            mapping_id=source.mapping_id,
            mapping_generation=source.mapping_generation,
            alias=alias,
            plan_digest=f"{identity:x}" * 64,
            plan={},
            state="running",
            route_state=route_state,
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(run)
        session.flush()
        session.add(
            RunNode(
                run_id=run.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="running",
                port=8000,
                reserved_memory_bytes=100,
                endpoint={"url": f"http://10.0.0.{identity}:8000"},
                evidence_digest=f"{identity:x}" * 64,
                updated_at=NOW,
            )
        )
        return run.id


def atomic_service(
    service: RecipeRouteService, root: Path, clock
) -> RecipeRouteService:
    runtime = AtomicRouteBundlePublisher(
        root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
    )
    return RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(runtime, clock=clock),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=300,
    )


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


def test_hermes_alias_comes_only_from_published_v1_recipe_run(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path,
        run_alias="hermes-agent",
        runtime_model_aliases=("deepseek-v4-flash-dspark",),
    )

    service.publish_run(run_id)

    document = json.loads(applied[-1])
    assert [row["model_name"] for row in document["model_list"]] == [
        "hermes-agent"
    ]
    assert (
        document["model_list"][0]["litellm_params"]["model"]
        == "openai/deepseek-v4-flash-dspark"
    )


def test_artifact_interface_never_publishes_a_litellm_route(tmp_path: Path) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path, interfaces=[{"adapter": "video-job"}]
    )

    with pytest.raises(RecipeRouteError, match="LiteLLM interface"):
        service.publish_run(run_id)

    assert applied == []


def test_missing_runtime_model_authority_blocks_route_publication(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, runtime_model_aliases=())

    with pytest.raises(RecipeRouteError, match="model authority"):
        service.publish_run(run_id)

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
    with pytest.raises(RecipeRouteError):
        service.publish_run(run_id)
    assert applied == []


def test_candidate_rank_identity_must_exactly_match_accepted_plan(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        run.plan = {
            "nodes": [
                {
                    "node_id": "spk_" + "9" * 32,
                    "rank": rank,
                    "role": "entrypoint" if rank == 0 else "worker",
                }
                for rank in range(2)
            ]
        }

    with pytest.raises(RecipeRouteError, match="accepted plan"):
        service.publish_run(run_id)
    assert applied == []


def test_invalid_candidate_retains_previous_generation(tmp_path: Path) -> None:
    service, publisher, _applied, run_id = setup(tmp_path)
    accepted = service.publish_run(run_id)
    rejecting = RecipeRouteService(
        service.sessions,
        publisher=LiteLlmPublisher(
            tmp_path / "litellm", validate=lambda _: False, apply=lambda _: None
        ),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=300,
    )
    with pytest.raises(LiteLlmPolicyError):
        rejecting.publish_run(run_id)
    assert publisher.active() == accepted


def test_withdraw_publishes_empty_generation_before_workload_stop(
    tmp_path: Path,
) -> None:
    service, publisher, applied, run_id = setup(tmp_path)
    service.publish_run(run_id)
    empty = service.withdraw_run(run_id)

    assert empty.generation == 2
    assert json.loads(applied[-1])["model_list"] == []
    assert publisher.active() == empty


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
            future.result(timeout=5)

    with service.sessions() as session:
        assert [
            session.get(RecipeRun, run_id).route_state
            for run_id in (first_run, second_run)
        ] == ["withdrawn", "withdrawn"]
    assert overlap.aliases[-1] == ()


def test_route_publication_owner_lock_compiles_for_postgresql() -> None:
    statement_factory = getattr(
        recipe_routes, "route_publication_owner_lock_statement", None
    )
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


def test_worker_publishes_pending_route_and_records_failure(tmp_path: Path) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        session.get(RecipeRun, run_id).route_state = "pending"
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: NOW)

    assert worker.tick() is True
    assert worker.tick() is False
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run.route_state == "published"
        assert run.route_generation == 1

    failed_service, _publisher, _applied, failed_run = setup(
        tmp_path / "failed", validate=lambda _: False
    )
    with failed_service.sessions.begin() as session:
        session.get(RecipeRun, failed_run).route_state = "pending"
    RecipeOperationWorker(
        failed_service.sessions, failed_service, clock=lambda: NOW
    ).tick()
    with failed_service.sessions() as session:
        failed = session.get(RecipeRun, failed_run)
        assert failed.route_state == "failed"
        assert "LiteLlmPolicyError" in failed.route_error


def test_atomic_adapter_keeps_caddy_routes_static_and_activates_litellm(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path / "database")
    atomic = AtomicRouteBundlePublisher(
        tmp_path / "live",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    service = RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(atomic, clock=lambda: NOW),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=300,
    )

    generation = service.publish_run(run_id)
    assert generation.generation == 1
    # Activation names are checksum-bound; use the active marker's exact name.
    marker = json.loads((tmp_path / "live" / "activation.json").read_text())
    directory = tmp_path / "live" / "generations" / marker["directory"]
    routes = json.loads((directory / "routes.json").read_text())
    assert routes["generation"] == generation.generation
    assert routes["state"] == "published"
    assert routes["routes"]["qwen"] == {
        "address": "10.0.0.2",
        "evidence_digest": "1" * 64,
        "node_id": "spk_" + "1".zfill(32),
        "observed_at": NOW.isoformat(),
        "operation_id": f"recipe:{run_id}:rank:0",
        "path": "/v1",
        "port": 8000,
        "scheme": "http",
        "verify_evidence_digest": "1" * 64,
    }
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
        publication = session.get(RoutePublication, owner.reconciliation_id)
        reconciliation = session.get(Reconciliation, owner.reconciliation_id)
        assert publication is not None and publication.state == "completed"
        assert reconciliation is not None and reconciliation.status == "succeeded"

    projection = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    endpoint = projection.endpoint("qwen")
    assert endpoint["api_base"] == "http://10.0.0.2:8000/v1"
    assert endpoint["node_id"] == "spk_" + "1".zfill(32)


def test_worker_renews_from_fresh_all_rank_evidence_and_recovers_owner(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    first = service.publish_run(run_id)

    clock.now = NOW + timedelta(seconds=240)
    with service.sessions.begin() as session:
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        for index, node in enumerate(nodes):
            node.updated_at = clock.now
            node.evidence_digest = f"{index + 5}" * 64
        session.delete(session.get(RoutePublicationOwner, 1))

    restarted = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert restarted.tick() is True
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        owner = session.get(RoutePublicationOwner, 1)
        publication = session.get(RoutePublication, owner.reconciliation_id)
        assert run.route_generation > first.generation
        assert publication.lease_expires_at.replace(tzinfo=UTC) == (
            clock.now + timedelta(seconds=300)
        )


def test_fresh_health_timestamp_does_not_churn_route_before_renewal_window(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    first = service.publish_run(run_id)

    clock.now += timedelta(seconds=1)
    with service.sessions.begin() as session:
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.updated_at = clock.now

    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is False
    with service.sessions() as session:
        assert session.get(RecipeRun, run_id).route_generation == first.generation


def test_worker_withdraws_when_rank_health_is_stale_while_agent_is_active(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(run_id)

    clock.now = NOW + timedelta(seconds=301)
    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert worker.tick() is True

    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        agents = tuple(session.get(AgentNode, node.node_id) for node in nodes)
        assert run.route_state == "withdrawn"
        assert all(agent is not None and agent.state == "active" for agent in agents)
        owner = session.get(RoutePublicationOwner, 1)
        publication = session.get(RoutePublication, owner.reconciliation_id)
        assert publication.state == "routes-withdrawn"


def test_worker_republishes_automatically_with_fresh_recovered_rank_evidence(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    first = service.publish_run(run_id)
    with service.sessions.begin() as session:
        failed = session.query(RunNode).filter_by(run_id=run_id, rank=1).one()
        failed.state = "failed"
        failed.updated_at = clock.now

    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert worker.tick() is True
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run.route_state == "withdrawn"
        assert run.route_error == "recipe rank health requires recovery"

    clock.now += timedelta(seconds=1)
    with service.sessions.begin() as session:
        recovered = session.query(RunNode).filter_by(run_id=run_id, rank=1).one()
        recovered.state = "running"
        recovered.updated_at = clock.now

    restarted = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert restarted.tick() is True
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run.route_state == "published"
        assert run.route_error is None
        assert run.route_generation > first.generation


def test_recovered_run_rejoins_candidate_while_another_run_remains_published(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, healthy_run = setup(tmp_path / "database", clock=clock)
    recovered_run = add_running_run(
        base,
        healthy_run,
        alias="recovered",
        route_state="pending",
        identity=3,
    )
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(healthy_run)
    service.publish_run(recovered_run)
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=recovered_run).one()
        node.state = "failed"

    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    assert worker.tick() is True
    with service.sessions() as session:
        assert session.get(RecipeRun, healthy_run).route_state == "published"
        assert session.get(RecipeRun, recovered_run).route_state == "withdrawn"

    clock.now += timedelta(seconds=1)
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=recovered_run).one()
        node.state = "running"
        node.updated_at = clock.now

    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    with service.sessions() as session:
        assert session.get(RecipeRun, healthy_run).route_state == "published"
        assert session.get(RecipeRun, recovered_run).route_state == "published"


def test_worker_withdraws_all_stale_runs_in_one_recovered_candidate(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, first_run = setup(tmp_path / "database", clock=clock)
    second_run = add_running_run(
        base,
        first_run,
        alias="second",
        route_state="pending",
        identity=3,
    )
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(first_run)
    service.publish_run(second_run)

    clock.now = NOW + timedelta(seconds=301)
    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True

    with service.sessions() as session:
        assert {
            session.get(RecipeRun, first_run).route_state,
            session.get(RecipeRun, second_run).route_state,
        } == {"withdrawn"}
        publication = session.get(RoutePublication, _recipe_owner_id(session))
        assert publication.state == "routes-withdrawn"


def _recipe_owner_id(session) -> str:
    owner = session.get(RoutePublicationOwner, 1)
    assert owner is not None and owner.reconciliation_id is not None
    return owner.reconciliation_id
