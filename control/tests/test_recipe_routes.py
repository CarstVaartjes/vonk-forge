from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.route_activation import ROUTE_LEASE_SECONDS
from vonk_control import recipe_routes
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profile_contract import (
    FleetProfileEndpointAssignmentIntent,
    FleetProfileEndpointIntent,
)
from vonk_control.litellm import (
    LiteLlmGeneration,
    LiteLlmPolicyError,
    LiteLlmPublisher,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRouteAuthority,
    RecipeRun,
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
    RecipeRouteNotReady,
    RecipeRouteService,
)
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _recipe_run(session: Session, run_id: str) -> RecipeRun:
    """Return the run row the test itself wrote, or fail on a missing row."""

    run = session.get(RecipeRun, run_id)
    assert run is not None
    return run


def _publication_owner(session: Session) -> RoutePublicationOwner:
    """Return the singleton route publication owner, which must exist here."""

    owner = session.get(RoutePublicationOwner, 1)
    assert owner is not None
    return owner


def _publication(session: Session, authority_id: str | None) -> RoutePublication:
    """Return the publication for an owner's authority, which must exist here."""

    publication = session.get(RoutePublication, authority_id)
    assert publication is not None
    return publication


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
    endpoint_owner_rank=0,
    exact_distributed=True,
    engine=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if engine is None:
        engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    nodes = tuple("spk_" + f"{index + 1:032x}" for index in range(ranks))
    if not 0 <= endpoint_owner_rank < ranks:
        raise ValueError("endpoint owner rank is outside the fixture")
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
        model = ModelDefinition.model_validate(
            json.loads(
                files("vonk_forge_contracts")
                .joinpath("examples", "model-definition.json")
                .read_text(encoding="utf-8")
            )
        )
        recipe_document = json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-image.json")
            .read_text(encoding="utf-8")
        )
        recipe_document["identity"]["slug"] = "qwen"
        recipe_document["metadata"]["title"] = "Qwen"
        recipe_document["models"][0]["model"]["content_sha256"] = content_sha256(model)
        if interfaces is None:
            recipe_document["interfaces"] = [
                {
                    "adapter": "openai",
                    "port": 8000,
                    "model_aliases": list(runtime_model_aliases) or ["invalid alias"],
                    "health_path": "/v1/models",
                }
            ]
        elif interfaces == [{"adapter": "video-job"}]:
            recipe_document["interfaces"] = [
                {
                    "adapter": "video-job",
                    "path": "/outputs",
                    "output": {
                        "path": "/outputs",
                        "max_total_bytes": 1,
                        "slots": [
                            {
                                "id": "output",
                                "label": "Output",
                                "description": "Rendered output",
                                "media_types": ["video/mp4"],
                                "extensions": [".mp4"],
                                "min_files": 1,
                                "max_files": 1,
                                "max_file_bytes": 1,
                                "max_total_bytes": 1,
                            }
                        ],
                    },
                }
            ]
            recipe_document["settings"] = {"kind": "job", "knobs": {}}
            recipe_document["validation"]["serving"] = {
                "interface": "video-job",
                "checks": [
                    {
                        "name": "output",
                        "kind": "video-job.output",
                        "request": {
                            "transport": "job",
                            "fixture": "fixture",
                            "output_path": "/outputs",
                            "output_slot": "output",
                        },
                        "assertions": ["artifact.output"],
                    }
                ],
            }
        else:
            recipe_document["interfaces"] = interfaces
        recipe = RecipeDefinition.model_validate(recipe_document)
        recipe_document = recipe.model_dump(mode="json")
        recipe_id = str(uuid4())
        revision_id = str(uuid4())
        model_id = str(uuid4())
        model_revision_id = str(uuid4())
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.flush()
        revision = CatalogDocumentRevision(
            id=revision_id,
            document_id=recipe_id,
            kind="recipe",
            publisher=recipe.identity.publisher,
            slug=recipe.identity.slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document=recipe_document,
            content_digest=content_sha256(recipe),
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add_all(
            [
                revision,
                CatalogDocumentRevision(
                    id=model_revision_id,
                    document_id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=content_sha256(model),
                    projected={},
                    created_by="admin",
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name=f"{ranks}-node",
            generation=1,
            node_count=ranks,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=nodes[endpoint_owner_rank],
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
                role="entrypoint" if rank == endpoint_owner_rank else "worker",
                endpoint_owner=rank == endpoint_owner_rank,
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
                role="entrypoint" if rank == endpoint_owner_rank else "worker",
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
            plan={
                "schema_version": 1,
                "observation_schema_version": 2,
                "run_generation": 1,
                "installation_id": installation.id,
                "alias": run_alias,
                "mapping_id": mapping.id,
                "mapping_generation": 1,
                "recipe_revision_id": revision.id,
                "plan_digest": "c" * 64,
                "nodes": [
                    {
                        "node_id": node,
                        "rank": rank,
                        "role": "entrypoint"
                        if rank == endpoint_owner_rank
                        else "worker",
                        "endpoint_owner": rank == endpoint_owner_rank,
                        "port": 8000,
                        "allowed": True,
                        "inventory_observed_at": NOW.isoformat(),
                        "memory_kind": "unified",
                        "memory_pool": "shared",
                        "required_memory_bytes": 100,
                        "available_memory_bytes": None,
                        "active_reserved_bytes": 0,
                        "free_after_bytes": None,
                        "memory_floor_bytes": 0,
                        "fabric_address": None,
                        "fabric_bandwidth_mbps": None,
                        "rendezvous_port": None,
                        "blockers": [],
                        "warnings": [],
                    }
                    for rank, node in enumerate(nodes)
                ],
            },
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
                    role="entrypoint" if rank == endpoint_owner_rank else "worker",
                    state="failed" if failed_rank and rank == ranks - 1 else "running",
                    port=8000,
                    reserved_memory_bytes=100,
                    endpoint={"url": f"http://10.0.0.{rank + 2}:8000"},
                    evidence_digest=str(rank + 1) * 64,
                    observed_run_generation=(
                        run.run_generation if exact_distributed else None
                    ),
                    observation_receipt_sha256=(
                        str(rank + 1) * 64 if exact_distributed else None
                    ),
                    observation_endpoint_ready=(
                        True
                        if exact_distributed and rank == endpoint_owner_rank
                        else None
                    ),
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
        maximum_age_seconds=120,
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
        source_installation = session.get(RecipeInstallation, source.installation_id)
        assert source_installation is not None
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=source_installation.recipe_revision_id,
            topology_name="single-node",
            generation=1,
            node_count=1,
            state="ready",
            parameters={},
            placement_digest=f"{identity:x}" * 64,
            endpoint_owner_node_id=node_id,
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
        session.add(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        installation = RecipeInstallation(
            recipe_revision_id=source_installation.recipe_revision_id,
            mapping_id=mapping.id,
            mapping_generation=1,
            recipe_build_id=source_installation.recipe_build_id,
            image_digest=source_installation.image_digest,
            plan_digest=f"{identity + 4:x}" * 64,
            plan={},
            state="installed",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(installation)
        session.flush()
        session.add(
            InstallationNode(
                installation_id=installation.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=NOW,
            )
        )
        run = RecipeRun(
            installation_id=installation.id,
            mapping_id=mapping.id,
            mapping_generation=mapping.generation,
            alias=alias,
            plan_digest=f"{identity:x}" * 64,
            plan={
                "schema_version": 1,
                "observation_schema_version": 2,
                "run_generation": 1,
                "installation_id": installation.id,
                "alias": alias,
                "mapping_id": mapping.id,
                "mapping_generation": mapping.generation,
                "recipe_revision_id": source_installation.recipe_revision_id,
                "plan_digest": f"{identity:x}" * 64,
                "nodes": [
                    {
                        "node_id": node_id,
                        "rank": 0,
                        "role": "entrypoint",
                        "endpoint_owner": True,
                        "port": 8000,
                        "allowed": True,
                        "inventory_observed_at": NOW.isoformat(),
                        "memory_kind": "unified",
                        "memory_pool": "shared",
                        "required_memory_bytes": 100,
                        "available_memory_bytes": None,
                        "active_reserved_bytes": 0,
                        "free_after_bytes": None,
                        "memory_floor_bytes": 0,
                        "fabric_address": None,
                        "fabric_bandwidth_mbps": None,
                        "rendezvous_port": None,
                        "blockers": [],
                        "warnings": [],
                    }
                ],
            },
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
                observed_run_generation=1,
                observation_receipt_sha256=f"{identity:x}" * 64,
                observation_endpoint_ready=True,
                updated_at=NOW,
            )
        )
        return run.id


def atomic_service(
    service: RecipeRouteService, root: Path, clock
) -> RecipeRouteService:
    runtime = AtomicRouteBundlePublisher(
        root,
        clock=clock,
    )
    return RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(runtime, clock=clock),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
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


def test_legacy_distributed_route_is_rejected_without_exact_local_rank_evidence(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path, exact_distributed=False)

    with pytest.raises(
        RecipeRouteError,
        match="exact observation",
    ):
        service.publish_run(run_id)

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

    with pytest.raises(RecipeRouteError, match="mapped endpoint-owner entrypoint"):
        service.publish_run(run_id)
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
    assert [row["model_name"] for row in document["model_list"]] == ["hermes-agent"]
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

    with pytest.raises(RecipeRouteError, match="stored recipe run plan is invalid"):
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
        maximum_age_seconds=120,
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
            _recipe_run(session, run_id).route_state
            for run_id in (first_run, second_run)
        ] == ["withdrawn", "withdrawn"]
    assert overlap.aliases[-1] == ()


def test_route_publication_owner_lock_compiles_for_postgresql() -> None:
    statement_factory = recipe_routes.route_publication_owner_lock_statement
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


class _TransientFirstPublish:
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

    def maintain(self, *, renew_before_seconds: int = 10) -> bool:
        return self._inner.maintain(renew_before_seconds=renew_before_seconds)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


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
    routes = _TransientFirstPublish(service, OSError("supervisor socket unavailable"))
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: now[0])

    assert worker.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "pending"
        assert run.route_attempts == 1
        assert "supervisor socket unavailable" in (run.route_error or "")
        recorded_due_at = run.route_next_attempt_at
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
        assert run.route_next_attempt_at is None
        assert run.route_error is None


def test_invalid_route_contract_is_not_retried_as_a_temporary_failure(
    tmp_path: Path,
) -> None:
    """An unrecognised route error stays one precise blocked reason."""

    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
    now = [NOW]
    routes = _TransientFirstPublish(
        service, RuntimeError("recipe route document is invalid")
    )
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: now[0])

    assert worker.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "failed"
        assert run.route_next_attempt_at is None
        assert run.route_error == "RuntimeError: recipe route document is invalid"

    now[0] = NOW + timedelta(hours=1)
    assert worker.tick() is False
    assert routes.attempts == 1


def test_route_publication_attempt_budget_ends_in_one_blocked_reason(
    tmp_path: Path,
) -> None:
    """A dependency that never returns exhausts, it does not retry forever."""

    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
    now = [NOW]
    routes = _TransientFirstPublish(
        service, OSError("supervisor socket unavailable"), failures=99
    )
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: now[0])

    for _ in range(20):
        if worker.tick() is False:
            break
        with service.sessions() as session:
            run = _recipe_run(session, run_id)
            if run.route_state != "pending":
                break
            pending_until = run.route_next_attempt_at
        assert pending_until is not None
        now[0] = _aware(pending_until) + timedelta(seconds=1)

    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "failed"
        assert run.route_attempts == 6
        assert run.route_next_attempt_at is None
        assert run.route_error is not None
        assert "did not converge after 6 attempts" in run.route_error

    # The exhausted run is no longer a publication candidate.
    assert worker.tick() is False


def test_acknowledgement_failure_after_activation_is_temporary() -> None:
    """An activated generation with a lost supervisor ack is retried.

    The generation exists, so the next attempt reconciles it instead of the
    Controller reporting a permanent route failure.
    """

    import vonk_control.recipe_routes as routes_module

    generation = LiteLlmGeneration(
        generation=1,
        route_digest="",
        config_sha256="",
        path="memory",
    )
    activated = routes_module._ActivatedRecipeRouteError(
        "recipe route activation acknowledgement failed",
        generation=generation,
    )

    assert routes_module.publication_is_temporary(activated) is True
    assert (
        routes_module.publication_is_temporary(RecipeRouteNotReady("waiting")) is True
    )
    assert routes_module.publication_is_temporary(OSError("socket")) is True
    assert routes_module.publication_is_temporary(LiteLlmPolicyError("bad")) is False
    assert (
        routes_module.publication_is_temporary(RuntimeError("invalid document"))
        is False
    )


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
        assert failed.route_state == "failed"
        assert failed.route_error is not None
        assert "LiteLlmPolicyError" in failed.route_error


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

        def maintain(self, *, renew_before_seconds: int = 10) -> bool:
            assert renew_before_seconds == 10
            self.maintained += 1
            return True

    routes = Routes()
    worker = RecipeOperationWorker(service.sessions, routes, clock=lambda: NOW)
    assert worker.tick() is True
    assert routes.published == [second_run]

    routes.ready.clear()
    assert worker.tick() is True
    assert routes.maintained == 1


def test_initial_exact_observation_deadline_fails_missing_rank_for_recovery(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.plan = {**run.plan, "observation_schema_version": 2}
        run.route_state = "pending"
        run.observation_deadline_at = NOW + timedelta(seconds=60)
        for node in session.query(RunNode).filter_by(run_id=run_id):
            node.observed_run_generation = None
            node.observation_receipt_sha256 = None
            node.observation_endpoint_ready = None
        session.add(
            Job(
                request_id="10000000-0000-4000-8000-000000000001",
                kind="recipe.start",
                state="succeeded",
                actor="admin",
                authority_revision="a" * 64,
                targets=[],
                payload_digest="b" * 64,
                payload={
                    "owner_id": run_id,
                    "start_deadline": (NOW + timedelta(seconds=60)).isoformat(),
                },
                result={},
                created_at=NOW,
                updated_at=NOW,
            )
        )

    class Recoveries:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return False

    recoveries = Recoveries()
    worker = RecipeOperationWorker(
        service.sessions,
        service,
        clock=lambda: NOW + timedelta(seconds=60),
        recoveries=recoveries,
    )
    assert worker.tick() is True
    assert recoveries.calls == 2
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        assert run.route_state == "withdrawn"
        assert run.route_error == "initial exact observation deadline elapsed"
        assert all(node.state == "failed" for node in nodes)


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
            node.observation_receipt_sha256 = "a" * 64
            node.observation_endpoint_ready = node.role == "entrypoint" or None
            node.updated_at = deadline + timedelta(microseconds=1)

    # Signed ingress has already enforced the first-receipt deadline. The
    # latest timestamp is a renewal and must retain its current health meaning.
    generation = service.publish_run(run_id)
    assert generation.generation == 1


def test_atomic_adapter_keeps_caddy_routes_static_and_activates_litellm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path / "database")
    atomic = AtomicRouteBundlePublisher(
        tmp_path / "live",
        clock=lambda: NOW,
    )
    service = RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(atomic, clock=lambda: NOW),
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
            application_state="succeeded",
            assignments=(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id="00000000-0000-4000-8000-000000000103",
                    recipe_title="Qwen",
                    desired_state="running",
                    alias="qwen",
                    state="not-published-yet" if published else "withdrawn",
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
    endpoint = projection.endpoint("qwen")
    assert endpoint["api_base"] == "http://10.0.0.2:8000/v1"
    assert endpoint["node_id"] == "spk_" + "1".zfill(32)

    profile_endpoint = projection.profile_endpoint(3, None)
    assert profile_endpoint.assignments is not None
    assert profile_endpoint.assignments[0].state == "published"
    assert profile_endpoint.assignments[0].endpoint is not None
    assert profile_endpoint.assignments[0].endpoint.generation == 1
    with pytest.raises(KeyError, match="other-profile"):
        projection.profile_endpoint(3, "other-profile")

    wrong_owner = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=lambda _session, number: FleetProfileEndpointIntent(
            number=number,
            profile_id="00000000-0000-4000-8000-000000000101",
            application_id="00000000-0000-4000-8000-000000000102",
            application_state="succeeded",
            assignments=(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id="00000000-0000-4000-8000-000000000103",
                    recipe_title="Qwen",
                    desired_state="running",
                    alias="qwen",
                    state="not-published-yet",
                    expected_run_id="00000000-0000-4000-8000-000000000104",
                ),
            ),
        ),
    )
    assert wrong_owner.profile_endpoint is not None
    wrong_owner_endpoint = wrong_owner.profile_endpoint(3, "qwen")
    assert wrong_owner_endpoint.assignments is not None
    assert wrong_owner_endpoint.assignments[0].state == "withdrawn"
    assert wrong_owner_endpoint.assignments[0].endpoint is None

    expired = durable_operation_services(
        service.sessions,
        tmp_path / "live",
        clock=lambda: NOW + timedelta(seconds=181),
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=profile_endpoint_intent,
    )
    assert expired.profile_endpoint is not None
    expired_endpoint = expired.profile_endpoint(3, "qwen")
    assert expired_endpoint.assignments is not None
    assert expired_endpoint.assignments[0].state == "expired"
    assert expired_endpoint.assignments[0].endpoint is None

    from vonk_control import operation_api

    verify_bundle = operation_api.verify_active_route_bundle
    renewed_during_verification = False
    replacement_generation = None

    def renew_after_bundle_verification(*args, **kwargs):
        nonlocal renewed_during_verification, replacement_generation
        bundle = verify_bundle(*args, **kwargs)
        if not renewed_during_verification:
            renewed_during_verification = True
            with service.sessions.begin() as session:
                for node in session.scalars(
                    select(RunNode).where(RunNode.run_id == run_id)
                ):
                    node.evidence_digest = "7" * 64
            replacement_generation = service.publish_run(run_id).generation
        return bundle

    # The alias and run remain valid, but the verified generation has changed.
    # Checking membership or published state alone would return stale evidence.
    with monkeypatch.context() as patch:
        patch.setattr(
            operation_api, "verify_active_route_bundle", renew_after_bundle_verification
        )
        with pytest.raises(RuntimeError, match="ownership changed during projection"):
            projection.profile_endpoint(3, "qwen")
    assert renewed_during_verification
    assert replacement_generation is not None
    assert replacement_generation > generation.generation
    current_endpoint = projection.profile_endpoint(3, "qwen")
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
            operation_api,
            "verify_active_route_bundle",
            withdraw_after_bundle_verification,
        )
        with pytest.raises(
            RuntimeError,
            match="active publication is unavailable|ownership changed during projection",
        ):
            projection.profile_endpoint(3, "qwen")
    assert withdrew_during_verification

    withdrawn = projection.profile_endpoint(3, "qwen")
    assert withdrawn.assignments is not None
    assert withdrawn.assignments[0].state == "withdrawn"
    assert withdrawn.assignments[0].endpoint is None


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
        run = _recipe_run(session, run_id)
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert (
            run.route_generation is not None and run.route_generation > first.generation
        )
        assert publication.lease_expires_at is not None
        assert publication.lease_expires_at.replace(tzinfo=UTC) == (
            clock.now + timedelta(seconds=180)
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
        assert _recipe_run(session, run_id).route_generation == first.generation


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
        run = _recipe_run(session, run_id)
        nodes = tuple(session.query(RunNode).filter_by(run_id=run_id))
        agents = tuple(session.get(AgentNode, node.node_id) for node in nodes)
        assert run.route_state == "withdrawn"
        assert all(agent is not None and agent.state == "active" for agent in agents)
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
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
        run = _recipe_run(session, run_id)
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
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        assert run.route_error is None
        assert (
            run.route_generation is not None and run.route_generation > first.generation
        )


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
        assert _recipe_run(session, healthy_run).route_state == "published"
        assert _recipe_run(session, recovered_run).route_state == "withdrawn"

    clock.now += timedelta(seconds=1)
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=recovered_run).one()
        node.state = "running"
        node.updated_at = clock.now

    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    with service.sessions() as session:
        assert _recipe_run(session, healthy_run).route_state == "published"
        assert _recipe_run(session, recovered_run).route_state == "published"


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
            _recipe_run(session, first_run).route_state,
            _recipe_run(session, second_run).route_state,
        } == {"withdrawn"}
        publication = _publication(session, _recipe_owner_id(session))
        assert publication.state == "routes-withdrawn"


def _recipe_owner_id(session) -> str:
    owner = session.get(RoutePublicationOwner, 1)
    assert owner is not None and owner.authority_id is not None
    return owner.authority_id


def _withdrawn_empty_publication(
    tmp_path: Path, *, engine: Engine, clock: MutableClock
) -> tuple[RecipeRouteService, Path, str, LiteLlmGeneration]:
    service, _publisher, _applied, run_id = setup(
        tmp_path / "database", clock=clock, engine=engine
    )
    root = tmp_path / "live"
    routes = atomic_service(service, root, clock)
    routes.publish_run(run_id)
    withdrawn = routes.withdraw_run(run_id)
    return routes, root, run_id, withdrawn


def test_postgres_current_publication_renewal_withdrawal_and_owner_recovery(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    from vonk_control.route_runtime import verify_active_route_bundle

    from .test_route_runtime import _supervisor

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(
        tmp_path / "database", clock=clock, engine=postgres_engine
    )
    root = tmp_path / "live"
    service = atomic_service(base, root, clock)
    first = service.publish_run(run_id)
    bundle = verify_active_route_bundle(root, clock=clock)
    request = _supervisor(monkeypatch, root)._active_request(now=clock.now)
    assert request is not None and request.activation_sha256 == bundle.marker.digest
    with service.sessions() as session:
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert publication.activation_marker == bundle.marker.model_dump()
        assert publication.activation_marker_digest == request.activation_sha256
        assert owner.owner_generation == first.generation

    clock.now += timedelta(seconds=240)
    with service.sessions.begin() as session:
        for index, node in enumerate(session.query(RunNode).filter_by(run_id=run_id)):
            node.updated_at = clock.now
            node.evidence_digest = str(index + 5) * 64
        session.delete(session.get(RoutePublicationOwner, 1))
    assert RecipeOperationWorker(service.sessions, service, clock=clock).tick() is True
    renewed = verify_active_route_bundle(root, clock=clock)
    assert renewed.marker.generation > first.generation
    with service.sessions() as session:
        owner = _publication_owner(session)
        assert owner.owner_generation == renewed.marker.generation

    service.withdraw_run(run_id)
    withdrawn = verify_active_route_bundle(root, clock=clock)
    assert withdrawn.marker.state == "maintenance"
    assert withdrawn.marker.generation > renewed.marker.generation
    assert withdrawn.routes["routes"] == {}
    assert _supervisor(monkeypatch, root)._active_request(now=clock.now) is not None


def test_postgres_expired_empty_route_renews_once_and_restores_supervisor_access(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    from vonk_control.route_runtime import verify_active_route_bundle

    from .test_route_runtime import _supervisor

    clock = MutableClock(NOW)
    routes, root, _run_id, withdrawn = _withdrawn_empty_publication(
        tmp_path, engine=postgres_engine, clock=clock
    )
    initial = verify_active_route_bundle(root, clock=clock).marker
    assert initial.generation == withdrawn.generation
    assert initial.state == "maintenance"

    # A healthy empty route stays stable until the configured renewal window.
    assert routes.maintain() is False
    assert AtomicRouteBundlePublisher(root, clock=clock).inspect() == initial

    with routes.sessions() as session:
        publication = _publication(session, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication.lease_expires_at is not None
        expires_at = publication.lease_expires_at.replace(tzinfo=UTC)
    clock.now = expires_at + timedelta(seconds=1)
    supervisor = _supervisor(monkeypatch, root)
    assert supervisor._active_request(now=clock.now) is None

    # Independent workers serialize through the real PostgreSQL owner row.
    # Only the first tick activates a new empty bundle.
    second_routes = atomic_service(routes, root, clock)
    workers = (
        RecipeOperationWorker(routes.sessions, routes, clock=clock),
        RecipeOperationWorker(second_routes.sessions, second_routes, clock=clock),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda worker: worker.tick(), workers)
        )
    assert sorted(results) == [False, True]

    renewed = verify_active_route_bundle(root, clock=clock)
    assert renewed.marker.generation == initial.generation + 1
    assert renewed.marker.state == "maintenance"
    assert renewed.routes["routes"] == {}
    assert renewed.litellm["model_list"] == []
    with routes.sessions() as session:
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert owner.authority_id == RECIPE_ROUTE_AUTHORITY_ID
        assert owner.owner_generation == renewed.marker.generation
        assert publication.state == "routes-withdrawn"
        assert publication.lease_expires_at is not None
        assert publication.lease_expires_at.replace(tzinfo=UTC) == (
            clock.now + timedelta(seconds=ROUTE_LEASE_SECONDS)
        )
    request = supervisor._active_request(now=clock.now)
    assert request is not None
    assert request.marker["generation"] == renewed.marker.generation


@pytest.mark.parametrize(
    "owner_change", ["foreign-authority", "newer-generation", "foreign-activation"]
)
def test_postgres_empty_route_renewal_preserves_newer_or_foreign_owner(
    tmp_path: Path, postgres_engine, owner_change: str
) -> None:
    from vonk_control.route_runtime import verify_active_route_bundle

    clock = MutableClock(NOW)
    routes, root, _run_id, _withdrawn = _withdrawn_empty_publication(
        tmp_path, engine=postgres_engine, clock=clock
    )
    runtime = AtomicRouteBundlePublisher(root, clock=clock)
    stored = verify_active_route_bundle(root, clock=clock).marker

    if owner_change == "foreign-activation":
        foreign_authority = str(uuid4())
        runtime.publish_compiled(
            authority_id=foreign_authority,
            plan_digest="f" * 64,
            evidence_set_digest="f" * 64,
            routes=b"{}\n",
            litellm=AtomicRouteBundlePublisher.empty_litellm(),
            expires_at=clock.now + timedelta(seconds=ROUTE_LEASE_SECONDS),
            state="maintenance",
        )
    else:
        with routes.sessions.begin() as session:
            owner = _publication_owner(session)
            publication = _publication(session, owner.authority_id)
            assert publication.generation is not None
            if owner_change == "foreign-authority":
                foreign_authority = str(uuid4())
                session.add(
                    RecipeRouteAuthority(
                        authority_id=foreign_authority,
                        created_at=clock.now,
                        updated_at=clock.now,
                    )
                )
                session.flush()
                owner.authority_id = foreign_authority
            else:
                owner.owner_generation = publication.generation + 1

    with routes.sessions() as session:
        publication = _publication(session, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication.lease_expires_at is not None
        clock.now = publication.lease_expires_at.replace(tzinfo=UTC) + timedelta(
            seconds=1
        )
    marker_before = runtime.inspect(verify_lease=False)

    assert routes.maintain() is False
    assert runtime.inspect(verify_lease=False) == marker_before
    with routes.sessions() as session:
        owner = _publication_owner(session)
        publication = _publication(session, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication.generation == stored.generation
        if owner_change == "foreign-authority":
            assert owner.authority_id != RECIPE_ROUTE_AUTHORITY_ID
        elif owner_change == "newer-generation":
            assert owner.owner_generation == stored.generation + 1
        else:
            assert owner.authority_id == RECIPE_ROUTE_AUTHORITY_ID
            assert owner.owner_generation == stored.generation


def test_postgres_empty_route_renewal_recovers_exact_unprojected_activation(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    from vonk_control.route_runtime import verify_active_route_bundle

    from .test_route_runtime import _supervisor

    clock = MutableClock(NOW)
    routes, root, _run_id, _withdrawn = _withdrawn_empty_publication(
        tmp_path, engine=postgres_engine, clock=clock
    )
    initial = verify_active_route_bundle(root, clock=clock).marker
    with routes.sessions() as session:
        publication = _publication(session, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication.evidence_digest is not None
        assert publication.lease_expires_at is not None
        route_digest = publication.evidence_digest
        clock.now = publication.lease_expires_at.replace(tzinfo=UTC) + timedelta(
            seconds=1
        )

    # Recreate the durable state after activation reached disk but before its
    # new generation and lease were projected into PostgreSQL.
    runtime = AtomicRouteBundlePublisher(root, clock=clock)
    AtomicRecipeRoutePublisher(runtime, clock=clock).publish_empty(
        route_digest,
        expires_at=clock.now + timedelta(seconds=ROUTE_LEASE_SECONDS),
    )
    unprojected = runtime.inspect(verify_lease=False)
    assert unprojected.generation == initial.generation + 1
    with routes.sessions() as session:
        assert _publication_owner(session).owner_generation == initial.generation

    assert routes.maintain() is True
    recovered = verify_active_route_bundle(root, clock=clock)
    assert recovered.marker == unprojected
    assert _supervisor(monkeypatch, root)._active_request(now=clock.now) is not None
    with routes.sessions() as session:
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert owner.owner_generation == unprojected.generation
        assert publication.generation == unprojected.generation
        assert publication.activation_marker_digest == unprojected.digest


def test_postgres_concurrent_current_publishers_keep_one_owner_receipt(
    tmp_path: Path, postgres_engine
) -> None:
    base, _, _, run_id = setup(tmp_path / "database", engine=postgres_engine)
    root = tmp_path / "live"
    services = [atomic_service(base, root, lambda: NOW) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.publish_run(run_id), services))
    with base.sessions() as session:
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        marker = AtomicRouteBundlePublisher(root, clock=lambda: NOW).inspect()
        assert owner.owner_generation == marker.generation == publication.generation
        assert publication.activation_marker_digest == marker.digest
        assert marker.generation == max(result.generation for result in results)


@pytest.mark.parametrize(
    ("failure_point", "recovering"),
    [
        ("before-activation", False),
        ("after-activation", False),
        ("after-activation", True),
    ],
)
def test_postgres_publication_recovers_after_worker_restart_without_new_effect(
    tmp_path: Path, postgres_engine, failure_point: str, recovering: bool
) -> None:
    """A lost publication response adopts an activated bundle on restart."""

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(
        tmp_path / "database", clock=clock, engine=postgres_engine
    )
    root = tmp_path / "live"
    acknowledgements: list[int] = []
    fail_ack = [failure_point == "after-activation"]

    def acknowledge(marker):
        acknowledgements.append(marker.generation)
        if fail_ack[0]:
            fail_ack[0] = False
            raise OSError("supervisor acknowledgement lost")

    runtime = AtomicRouteBundlePublisher(
        root, clock=clock, await_supervisor_ack=acknowledge
    )
    if failure_point == "before-activation":
        activate = runtime._activate
        fail_activate = [True]

        def activate_once(**kwargs):
            if fail_activate[0]:
                fail_activate[0] = False
                raise OSError("supervisor unavailable before activation")
            return activate(**kwargs)

        runtime._activate = activate_once
    service = RecipeRouteService(
        base.sessions,
        publisher=AtomicRecipeRoutePublisher(runtime, clock=clock),
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
        AtomicRouteBundlePublisher(root, clock=clock).inspect()
        if failure_point == "after-activation"
        else None
    )
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.state == "running"
        assert run.route_state == "pending"
        assert run.route_next_attempt_at is not None
        due_at = _aware(run.route_next_attempt_at)
        if recovering:
            recovery_job = session.query(Job).filter_by(kind="recipe.start").one()
            assert recovery_job.result == {}

    clock.now = due_at + timedelta(seconds=1)
    restarted_runtime = AtomicRouteBundlePublisher(
        root, clock=clock, await_supervisor_ack=acknowledge
    )
    restarted_service = RecipeRouteService(
        base.sessions,
        publisher=AtomicRecipeRoutePublisher(restarted_runtime, clock=clock),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )
    assert (
        RecipeOperationWorker(base.sessions, restarted_service, clock=clock).tick()
        is True
    )
    final_marker = AtomicRouteBundlePublisher(root, clock=clock).inspect()
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
        assert acknowledgements == [1, 1]
    else:
        assert final_marker.generation == 1
        assert acknowledgements == [1]
