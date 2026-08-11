from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
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
    RunNode,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import (
    AtomicRecipeRoutePublisher,
    RecipeRouteError,
    RecipeRouteService,
)
from vonk_control.route_runtime import AtomicRouteBundlePublisher

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def setup(
    tmp_path: Path, *, ranks=2, stale=False, failed_rank=False, validate=lambda _: True
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
                last_seen_at=NOW
                - (timedelta(seconds=301) if stale else timedelta()),
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
            document={},
            content_sha256="a" * 64,
            created_by="admin",
            created_at=NOW,
        )
        session.add(revision)
        session.flush()
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            profile_name=f"{ranks}-node",
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
            alias="qwen",
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
                    updated_at=NOW,
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
        clock=lambda: NOW,
        maximum_age_seconds=300,
    )
    return service, publisher, applied, run.id


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


def test_withdrawn_running_recipe_is_excluded_from_other_route_candidates(
    tmp_path: Path,
) -> None:
    service, _publisher, _applied, run_id = setup(tmp_path)
    service.publish_run(run_id)
    service.withdraw_run(run_id)

    state, included, policy = service._candidate(exclude_run_id=None)

    assert state.aliases == {}
    assert included == set()
    assert policy.models == {}


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


def test_worker_withdraws_on_rank_presence_loss_and_republishes_after_recovery(
    tmp_path: Path,
) -> None:
    service, _publisher, applied, run_id = setup(tmp_path)
    service.publish_run(run_id)
    worker = RecipeOperationWorker(service.sessions, service, clock=lambda: NOW)
    with service.sessions.begin() as session:
        failed_node = session.get(AgentNode, "spk_" + f"{2:032x}")
        assert failed_node is not None
        failed_node.last_seen_at = NOW - timedelta(seconds=301)

    assert worker.tick() is True
    assert json.loads(applied[-1])["model_list"] == []
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run.route_state == "withdrawn"
        assert run.route_error == "recipe rank presence is unavailable"

    with service.sessions.begin() as session:
        session.get(AgentNode, "spk_" + f"{2:032x}").last_seen_at = NOW

    assert worker.tick() is True
    assert json.loads(applied[-1])["model_list"][0]["model_name"] == "qwen"
    with service.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run.route_state == "published"
        assert run.route_error is None


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
    assert json.loads((directory / "routes.json").read_text())["routes"] == {}
    assert (
        json.loads((directory / "litellm.json").read_text())["model_list"][0][
            "model_name"
        ]
        == "qwen"
    )
