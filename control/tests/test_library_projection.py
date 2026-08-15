from __future__ import annotations

import json
import tracemalloc
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogEntity,
    CatalogEntityRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    NodeArtifact,
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "global"


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


def _node_id(value: int) -> str:
    return "spk_" + f"{value:032x}"


def test_mapping_preview_input_derives_one_topology_from_the_recipe_revision() -> None:
    """Break caught: callers can select a topology for a v1 recipe."""

    from vonk_control.library_contract import MappingPreviewInput

    preview = MappingPreviewInput.model_validate(
        {
            "recipe_revision_id": _uuid(1),
            "node_ids": [_node_id(1)],
            "parameters": {"max_model_len": 32_768},
        }
    )

    assert preview.model_dump() == {
        "recipe_revision_id": _uuid(1),
        "node_ids": [_node_id(1)],
        "parameters": {"max_model_len": 32_768},
    }


def _database() -> tuple[object, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def _document(*, family: str, nodes: int = 1, title: str = "Visual title") -> dict:
    document = json.loads((FIXTURES / "recipe-v1-minimal.json").read_text())
    document["model"]["slug"] = family.replace("/", "-")
    document["metadata"]["title"] = title
    document["metadata"]["description"] = f"{title} purpose"
    document["identity"]["slug"] = title.lower().replace(" ", "-")
    topology = document["topology"]
    if nodes != 1:
        topology["name"] = "pair" if nodes == 2 else f"nodes_{nodes}"
        topology["mode"] = "tensor_parallel"
        topology["node_count"] = nodes
        topology["parallelism"] = {
            "tensor": nodes,
            "pipeline": 1,
            "data": 1,
            "backend": "native",
        }
        topology["fabric"] = {
            "connectivity": "connected",
            "minimum_bandwidth_mbps": 10_000,
        }
        worker = deepcopy(topology["roles"][0])
        worker.update({"name": "worker", "count": nodes - 1, "endpoint_owner": False})
        topology["roles"].append(worker)
        topology["start_order"] = ["entrypoint", "worker"]
        topology["stop_order"] = ["worker", "entrypoint"]
        document["artifacts"][0]["roles"].append("worker")
    for artifact in document["artifacts"]:
        artifact["download_bytes"] = 100
        artifact["installed_bytes"] = 100
    for role in topology["roles"]:
        role["resources"]["disk"] = {
            "image_bytes": 100,
            "artifact_bytes": 100,
            "staging_bytes": 20,
            "cache_bytes": 10,
            "rollback_bytes": 0,
            "safety_margin_bytes": 50,
        }
        role["resources"]["memory"] = {
            "kind": "unified",
            "startup_peak_bytes": 100,
            "steady_state_bytes": 80,
            "runtime_growth_bytes": 20,
            "system_reserve_bytes": 20,
        }
    validate_recipe(document)
    return document


def _recipe(
    session: Session,
    value: int,
    *,
    slug: str,
    title: str,
    document: dict | None,
    revision_number: int = 1,
    lifecycle: str = "resolved",
    recipe_id: str | None = None,
) -> tuple[LocalRecipe, LocalRecipeRevision | None]:
    identifier = recipe_id or _uuid(value)
    recipe = session.get(LocalRecipe, identifier)
    if recipe is None:
        recipe = LocalRecipe(
            id=identifier,
            slug=slug,
            title=title,
            description=f"{title} catalog description",
            source_kind="local",
            created_by="operator",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(minutes=value),
        )
        session.add(recipe)
    if document is None:
        return recipe, None
    if "model" in document:
        _ensure_catalog_entities(session, document)
    revision = LocalRecipeRevision(
        id=_uuid(1_000 + value * 10 + revision_number),
        recipe_id=identifier,
        revision_number=revision_number,
        lifecycle=lifecycle,
        schema_version=1,
        document=deepcopy(document),
        content_sha256=(
            recipe_content_sha256(document) if lifecycle == "resolved" else None
        ),
        created_by="operator",
        created_at=NOW - timedelta(hours=10 - revision_number),
    )
    session.add(revision)
    return recipe, revision


def _ensure_catalog_entities(session: Session, document: dict) -> None:
    def reference(kind: str, slug: str, digest: str) -> dict[str, str]:
        return {
            "kind": kind,
            "publisher": "vonk-forge",
            "slug": slug,
            "content_sha256": digest,
        }

    model_version = document["model"]
    harness = document["execution"]["harness"]
    distribution = document["runtime"]["distribution"]
    model = reference("model", "synthetic-tiny", "e" * 64)
    group = reference("model-group", "synthetic", "f" * 64)
    entities = (
        ("model-group", group, {}),
        ("model", model, {"model_group": group}),
        ("model-version", model_version, {"model": model}),
        ("execution-harness", harness, {}),
        (
            "runtime-distribution",
            distribution,
            {"implements_harness": harness},
        ),
    )
    for kind, identity, entity_document in entities:
        existing = session.scalar(
            select(CatalogEntityRevision)
            .join(CatalogEntity)
            .where(
                CatalogEntity.kind == kind,
                CatalogEntity.publisher == identity["publisher"],
                CatalogEntity.slug == identity["slug"],
                CatalogEntityRevision.content_sha256 == identity["content_sha256"],
            )
        )
        if existing is not None:
            continue
        entity = CatalogEntity(
            kind=kind,
            publisher=identity["publisher"],
            slug=identity["slug"],
            title=f"{kind} {identity['slug']}",
            created_by="operator",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(entity)
        session.flush()
        session.add(
            CatalogEntityRevision(
                entity_id=entity.id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document=entity_document,
                content_sha256=identity["content_sha256"],
                created_by="operator",
                created_at=NOW,
            )
        )


def _node(
    session: Session,
    value: int,
    *,
    telemetry_age: int | None = 2,
    inventory_age: int | None = 10,
    disk_free: int = 1_000,
    memory_free: int = 1_000,
    fabric_address: str | None = None,
    fabric_bandwidth: int | None = 100_000,
    capabilities: list[str] | None = None,
    state: str = "active",
    architecture: str = "linux-arm64",
) -> str:
    node_id = _node_id(value)
    session.add(
        AgentNode(
            node_id=node_id,
            state=state,
            architecture=architecture,
            capabilities=capabilities
            or ["runtime.vonk.v1", "fabric.full_mesh.mbps.100000"],
            last_seen_at=NOW - timedelta(seconds=2),
        )
    )
    session.flush()
    if inventory_age is not None:
        address = fabric_address if fabric_address is not None else f"10.0.0.{value}"
        session.add(
            NodeInventorySnapshot(
                id=_uuid(10_000 + value),
                node_id=node_id,
                observed_at=NOW - timedelta(seconds=inventory_age),
                received_at=NOW - timedelta(seconds=max(0, inventory_age - 1)),
                disk_total_bytes=2_000,
                disk_free_bytes=disk_free,
                host_memory_total_bytes=2_000,
                host_memory_free_bytes=memory_free,
                gpu_memory_total_bytes=2_000,
                gpu_memory_free_bytes=memory_free,
                gpu_count=1,
                fabric_address=address,
                fabric_bandwidth_mbps=(
                    fabric_bandwidth if address is not None else None
                ),
                nvidia_driver_version="580.1",
                container_runtime_version="1.2.3",
                artifact_store_read_only=False,
                capabilities=capabilities
                or ["runtime.vonk.v1", "fabric.full_mesh.mbps.100000"],
                evidence_digest=f"{value:064x}",
            )
        )
    if telemetry_age is not None:
        sample = NodeTelemetrySample(
            id=_uuid(20_000 + value),
            node_id=node_id,
            boot_id=_uuid(30_000 + value),
            sequence=1,
            observed_at=NOW - timedelta(seconds=telemetry_age),
            received_at=NOW - timedelta(seconds=max(0, telemetry_age - 1)),
            cpu_utilization_percent=10.0,
            load_average_1m=1.0,
            memory_total_bytes=2_000,
            memory_available_bytes=memory_free,
            disk_total_bytes=2_000,
            disk_free_bytes=disk_free,
            gpu_utilization_percent=20.0,
            gpu_memory_total_bytes=2_000,
            gpu_memory_free_bytes=memory_free,
            temperature_c=40.0,
            power_watts=20.0,
            network_receive_bytes_per_second=100.0,
            network_transmit_bytes_per_second=100.0,
            gap_samples=0,
            details={},
        )
        session.add(sample)
        session.flush()
        session.add(NodeTelemetryLatest(node_id=node_id, sample_id=sample.id))
    return node_id


def _projection(sessions: sessionmaker[Session]) -> LibraryProjection:
    return LibraryProjection(
        sessions,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        clock=lambda: NOW,
        disk_floor_bytes=50,
        memory_floor_bytes=50,
    )


def _run_status_service(
    sessions: sessionmaker[Session],
) -> RecipeOperationService:
    admission = RunAdmissionService(
        sessions,
        inventory_max_age=300,
        memory_floor_bytes=50,
    )
    return RecipeOperationService(
        sessions,
        install_admission=object(),  # type: ignore[arg-type]
        run_admission=admission,
        agent_jobs=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def _operational_group(
    session: Session,
    revision: LocalRecipeRevision,
    node_ids: tuple[str, ...],
    *,
    value: int,
    installed: bool = True,
    running: bool = False,
) -> dict[str, str]:
    topology = revision.document["topology"]
    expanded_roles = [
        (role["name"], role["endpoint_owner"])
        for role in topology["roles"]
        for _ in range(role["count"])
    ]
    mapping = ClusterMapping(
        id=_uuid(40_000 + value),
        recipe_revision_id=revision.id,
        topology_name=topology["name"],
        generation=1,
        node_count=len(node_ids),
        state="ready",
        parameters={},
        placement_digest=f"{40_000 + value:064x}",
        endpoint_owner_node_id=min(node_ids),
        created_by="operator",
        created_at=NOW - timedelta(minutes=4),
        updated_at=NOW - timedelta(minutes=4),
    )
    build = RecipeBuild(
        id=_uuid(41_000 + value),
        recipe_revision_id=revision.id,
        builder_node_id=min(node_ids),
        source_bundle_sha256="a" * 64,
        build_input_sha256=f"{41_000 + value:064x}",
        state="succeeded",
        policy_report={},
        plan={},
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=100,
        error=None,
        created_at=NOW - timedelta(minutes=3),
        updated_at=NOW - timedelta(minutes=3),
    )
    installation = RecipeInstallation(
        id=_uuid(42_000 + value),
        recipe_revision_id=revision.id,
        mapping_id=mapping.id,
        mapping_generation=1,
        recipe_build_id=build.id,
        image_digest=build.image_digest,
        plan_digest=f"{42_000 + value:064x}",
        plan={
            "schema_version": 1,
            "nodes": [
                {"node_id": node_id, "rank": rank, "role": role}
                for rank, (node_id, (role, _endpoint_owner)) in enumerate(
                    zip(sorted(node_ids), expanded_roles, strict=True)
                )
            ],
        },
        state="installed" if installed else "partial",
        actor="operator",
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=2),
    )
    session.add_all([mapping, build, installation])
    session.flush()
    for rank, (node_id, (role, endpoint_owner)) in enumerate(
        zip(sorted(node_ids), expanded_roles, strict=True)
    ):
        session.add(
            ClusterMappingNode(
                id=_uuid(43_000 + value * 10 + rank),
                mapping_id=mapping.id,
                node_id=node_id,
                rank=rank,
                role=role,
                endpoint_owner=endpoint_owner,
                created_at=NOW - timedelta(minutes=4),
            )
        )
        session.add(
            InstallationNode(
                id=_uuid(44_000 + value * 10 + rank),
                installation_id=installation.id,
                node_id=node_id,
                rank=rank,
                role=role,
                state="installed" if installed or rank == 0 else "installing",
                required_bytes=100,
                installed_bytes=100 if installed or rank == 0 else 40,
                evidence_digest="d" * 64,
                updated_at=NOW - timedelta(minutes=1),
            )
        )
    result = {
        "mapping_id": mapping.id,
        "recipe_build_id": build.id,
        "installation_id": installation.id,
    }
    if running:
        run = RecipeRun(
            id=_uuid(45_000 + value),
            installation_id=installation.id,
            mapping_id=mapping.id,
            mapping_generation=1,
            alias=f"recipe-{value}",
            plan_digest=f"{45_000 + value:064x}",
            plan={
                "schema_version": 1,
                "nodes": [
                    {
                        "node_id": node_id,
                        "rank": rank,
                        "role": role,
                    }
                    for rank, (node_id, (role, _endpoint_owner)) in enumerate(
                        zip(sorted(node_ids), expanded_roles, strict=True)
                    )
                ],
            },
            state="running",
            route_state="published",
            actor="operator",
            created_at=NOW - timedelta(seconds=30),
            updated_at=NOW - timedelta(seconds=2),
        )
        session.add(run)
        session.flush()
        for rank, (node_id, (role, _endpoint_owner)) in enumerate(
            zip(sorted(node_ids), expanded_roles, strict=True)
        ):
            session.add(
                RunNode(
                    id=_uuid(46_000 + value * 10 + rank),
                    run_id=run.id,
                    node_id=node_id,
                    rank=rank,
                    role=role,
                    state="running",
                    port=8_000 + rank,
                    reserved_memory_bytes=120,
                    observed_memory_bytes=80,
                    endpoint=None,
                    evidence_digest="e" * 64,
                    updated_at=NOW - timedelta(seconds=2),
                )
            )
        result["run_id"] = run.id
    return result


def test_root_groups_page_local_families_and_paginates_every_recipe_once() -> None:
    _engine, sessions = _database()
    with sessions.begin() as session:
        _recipe(
            session,
            1,
            slug="alpha",
            title="Alpha human title",
            document=_document(family="shared-model", title="Alpha visual"),
        )
        _recipe(
            session,
            2,
            slug="bravo",
            title="Bravo human title",
            document=_document(family="shared-model", title="Bravo visual"),
        )
        _recipe(
            session,
            3,
            slug="charlie",
            title="Charlie human title",
            document={},
            lifecycle="draft",
        )
        _recipe(
            session,
            4,
            slug="delta",
            title="Delta human title",
            document=None,
        )

    projection = _projection(sessions)
    pages = []
    cursor = None
    while True:
        page = projection.list(limit=2, cursor=cursor)
        pages.append(page)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    visible = [
        recipe for page in pages for model in page.models for recipe in model.recipes
    ] + [recipe for page in pages for recipe in page.unlinked_recipes]
    assert [recipe.slug for recipe in visible] == ["alpha", "bravo", "charlie", "delta"]
    assert len({recipe.recipe_id for recipe in visible}) == 4
    assert [model.family for page in pages for model in page.models] == ["shared-model"]
    assert all(model.page_local is True for page in pages for model in page.models)
    assert [recipe.title for recipe in visible[:2]] == [
        "Alpha human title",
        "Bravo human title",
    ]
    assert visible[2].reasons[0].code == "recipe.document_invalid"
    assert visible[3].reasons[0].code == "recipe.unresolved"
    assert pages[-1].next_cursor is None
    assert pages[0].freshness_policy.model_dump() == {
        "inventory_fresh_seconds": 300,
        "telemetry_live_seconds": 6,
        "telemetry_delayed_seconds": 20,
    }

    first_again = projection.list(limit=2, cursor=None)
    assert first_again.next_cursor == pages[0].next_cursor
    with pytest.raises(ValueError, match="cursor"):
        projection.list(limit=3, cursor=pages[0].next_cursor)
    with pytest.raises(ValueError, match="cursor"):
        projection.list(
            limit=2,
            cursor=pages[0].next_cursor[:-1]
            + ("A" if pages[0].next_cursor[-1] != "A" else "B"),
        )


def test_root_operational_summaries_are_exact_bounded_and_fair_per_recipe() -> None:
    engine, sessions = _database()
    with sessions.begin() as session:
        _recipe(
            session,
            5,
            slug="alpha-history-heavy",
            title="History heavy",
            document=_document(family="summary-model", title="History heavy"),
        )
        _recipe(
            session,
            6,
            slug="bravo-current",
            title="Current recipe",
            document=_document(family="summary-model", title="Current recipe"),
        )
        alpha_revision = session.get(LocalRecipeRevision, _uuid(1_051))
        bravo_revision = session.get(LocalRecipeRevision, _uuid(1_061))
        alpha_node = _node(session, 90)
        bravo_node = _node(session, 91)
        for value in range(65):
            _operational_group(
                session,
                alpha_revision,
                (alpha_node,),
                value=100 + value,
                installed=value == 0,
                running=value == 0,
            )
        bravo = _operational_group(
            session,
            bravo_revision,
            (bravo_node,),
            value=200,
            installed=True,
            running=True,
        )

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if " ".join(statement.split()).lower().startswith("select"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    snapshot = _projection(sessions).list()
    event.remove(engine, "before_cursor_execute", record)

    summaries = {item.slug: item for model in snapshot.models for item in model.recipes}
    alpha = summaries["alpha-history-heavy"]
    bravo_summary = summaries["bravo-current"]
    assert len(statements) == 6
    installation_window = next(
        statement
        for statement in statements
        if "recipe_installations" in statement and "row_number()" in statement
    )
    run_window = next(
        statement
        for statement in statements
        if "recipe_runs" in statement and "row_number()" in statement
    )
    assert "local_recipe_revisions.recipe_id IN" in installation_window
    assert "local_recipe_revisions.recipe_id IN" in run_window
    for table, parent in (
        ("cluster_mapping_nodes", "mapping_id IN"),
        ("installation_nodes", "installation_id IN"),
        ("run_nodes", "run_id IN"),
    ):
        membership_query = next(
            statement for statement in statements if table in statement
        )
        assert parent in membership_query
    assert alpha.installation_total_count == 65
    assert alpha.installation_returned_count == 64
    assert alpha.installations_truncated is True
    assert len(alpha.installations) == 64
    assert alpha.runs_truncated is False
    assert bravo_summary.installation_total_count == 1
    assert bravo_summary.installations[0].model_dump() == {
        "installation_id": bravo["installation_id"],
        "recipe_revision_id": bravo_revision.id,
        "state": "installed",
        "installed_rank_count": 1,
        "expected_rank_count": 1,
        "complete": True,
    }
    assert bravo_summary.runs[0].model_dump() == {
        "run_id": bravo["run_id"],
        "installation_id": bravo["installation_id"],
        "recipe_revision_id": bravo_revision.id,
        "state": "running",
        "route_state": "published",
        "healthy_rank_count": 1,
        "expected_rank_count": 1,
        "healthy": True,
    }
    reason = next(
        item
        for item in alpha.reasons
        if item.code == "recipe.operational_summary_truncated"
    )
    assert "installations: 65 total/64 returned" in reason.detail


def test_detail_selects_latest_revision_but_keeps_older_active_state_exact() -> None:
    _engine, sessions = _database()
    recipe_id = _uuid(10)
    old_document = _document(family="family-a", title="Old visual title")
    new_document = _document(family="family-a", title="New visual title")
    with sessions.begin() as session:
        _recipe(
            session,
            10,
            slug="revisioned",
            title="Catalog title",
            document=old_document,
            revision_number=1,
            recipe_id=recipe_id,
        )
        _recipe(
            session,
            10,
            slug="revisioned",
            title="Catalog title",
            document=new_document,
            revision_number=2,
            recipe_id=recipe_id,
        )
        old_revision = session.get(LocalRecipeRevision, _uuid(1_101))
        node_id = _node(session, 1)
        identifiers = _operational_group(
            session, old_revision, (node_id,), value=1, installed=True, running=True
        )

    detail = _projection(sessions).detail(recipe_id)

    assert detail.recipe.title == "Catalog title"
    assert detail.selected_revision.revision_number == 2
    assert detail.visual_recipe.metadata.title == "New visual title"
    assert detail.visual_recipe.model.slug == "family-a"
    assert detail.operational_state.installations[0].recipe_revision_id == _uuid(1_101)
    assert (
        detail.operational_state.installations[0].installation_id
        == identifiers["installation_id"]
    )
    assert detail.operational_state.runs[0].recipe_revision_id == _uuid(1_101)
    assert detail.operational_state.runs[0].run_id == identifiers["run_id"]
    assert detail.operational_state.mappings[0].mapping_id == identifiers["mapping_id"]
    assert (
        detail.operational_state.builds[0].recipe_build_id
        == identifiers["recipe_build_id"]
    )
    recommendation = detail.placement[0].recommendations[0]
    assert recommendation.recipe_revision_id == detail.selected_revision.id
    assert recommendation.mapping_id is None
    assert recommendation.recipe_build_id is None
    assert recommendation.installation_ids == []
    assert recommendation.run_ids == []
    assert [target.kind for target in recommendation.preview_targets] == ["mapping"]
    assert recommendation.preview_targets[0].input.recipe_revision_id == (
        detail.selected_revision.id
    )
    assert not hasattr(recommendation.preview_targets[0], "url")


def test_detail_keeps_a_recipe_without_revisions_visible_without_fabricated_ids() -> (
    None
):
    _engine, sessions = _database()
    with sessions.begin() as session:
        _recipe(
            session,
            15,
            slug="revision-missing",
            title="Revision missing title",
            document=None,
        )

    detail = _projection(sessions).detail(_uuid(15))

    assert detail.recipe.title == "Revision missing title"
    assert detail.selected_revision is None
    assert detail.visual_recipe is None
    assert detail.topology is None
    assert detail.placement == []
    assert detail.operational_state.model_dump() == {
        "builds": [],
        "mappings": [],
        "installations": [],
        "runs": [],
    }
    assert [reason.code for reason in detail.reasons] == ["recipe.unresolved"]


def test_placement_returns_deterministic_complete_pairs_and_prefers_exact_install() -> (
    None
):
    _engine, sessions = _database()
    document = _document(family="pair-model", nodes=2, title="Pair recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            20,
            slug="pair-recipe",
            title="Pair catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_201))
        node_c = _node(session, 3)
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_b, node_a),
            value=20,
            installed=True,
            running=True,
        )

    detail = _projection(sessions).detail(_uuid(20))
    placement = detail.placement[0]

    assert placement.node_count == 2
    assert placement.search_complete is True
    assert placement.evaluated_group_count == 3
    assert [group.node_ids for group in placement.recommendations] == [
        [node_a, node_b],
        [node_a, node_c],
        [node_b, node_c],
    ]
    assert all(group.group_complete for group in placement.recommendations)
    assert all(len(group.nodes) == 2 for group in placement.recommendations)
    assert [(node.rank, node.role) for node in placement.recommendations[0].nodes] == [
        (0, "entrypoint"),
        (1, "worker"),
    ]
    preferred = placement.recommendations[0]
    assert preferred.install_state == "complete"
    assert preferred.load_state == "loaded"
    assert preferred.mapping_id == identifiers["mapping_id"]
    assert preferred.recipe_build_id == identifiers["recipe_build_id"]
    assert preferred.installation_ids == [identifiers["installation_id"]]
    assert preferred.run_ids == [identifiers["run_id"]]
    assert [target.kind for target in preferred.preview_targets] == [
        "mapping",
        "install",
        "run",
    ]
    assert preferred.preview_targets[1].input.model_dump() == {
        "mapping_id": identifiers["mapping_id"],
        "recipe_build_id": identifiers["recipe_build_id"],
    }
    assert preferred.preview_targets[2].input.model_dump() == {
        "installation_id": identifiers["installation_id"]
    }
    assert "run.loaded" in {reason.code for reason in preferred.reasons}
    assert "placement.single_group_preview_required" in {
        reason.code for reason in preferred.reasons
    }
    assert not any("unload" in reason.detail.lower() for reason in preferred.reasons)


def test_exact_install_state_survives_a_newer_usable_build() -> None:
    _engine, sessions = _database()
    document = _document(family="build-history", title="Build history")
    with sessions.begin() as session:
        _recipe(
            session,
            25,
            slug="build-history",
            title="Build history catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_251))
        node_id = _node(session, 1)
        identifiers = _operational_group(
            session, revision, (node_id,), value=25, installed=True
        )
        newer_build = RecipeBuild(
            id=_uuid(41_999),
            recipe_revision_id=revision.id,
            builder_node_id=node_id,
            source_bundle_sha256="a" * 64,
            build_input_sha256="f" * 64,
            state="succeeded",
            policy_report={},
            plan={},
            image_digest="sha256:" + "9" * 64,
            oci_layout_sha256="8" * 64,
            image_bytes=100,
            error=None,
            created_at=NOW - timedelta(minutes=1),
            updated_at=NOW - timedelta(minutes=1),
        )
        session.add(newer_build)

    recommendation = (
        _projection(sessions).detail(_uuid(25)).placement[0].recommendations[0]
    )

    assert recommendation.install_state == "complete"
    assert recommendation.installation_ids == [identifiers["installation_id"]]
    assert recommendation.recipe_build_id == _uuid(41_999)
    assert recommendation.preview_targets[1].input.recipe_build_id == _uuid(41_999)


def test_verified_artifact_reuse_changes_the_stable_candidate_order() -> None:
    _engine, sessions = _database()
    document = _document(family="artifact-reuse", title="Artifact reuse")
    with sessions.begin() as session:
        _recipe(
            session,
            26,
            slug="artifact-reuse",
            title="Artifact reuse catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_261))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        build = RecipeBuild(
            id=_uuid(42_999),
            recipe_revision_id=revision.id,
            builder_node_id=node_a,
            source_bundle_sha256="a" * 64,
            build_input_sha256="7" * 64,
            state="succeeded",
            policy_report={},
            plan={},
            image_digest="sha256:" + "6" * 64,
            oci_layout_sha256="5" * 64,
            image_bytes=100,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )
        artifact = document["artifacts"][0]
        session.add(build)
        session.add_all(
            [
                NodeArtifact(
                    id=_uuid(60_001),
                    node_id=node_b,
                    kind="image",
                    digest="6" * 64,
                    source="local-build",
                    size_bytes=100,
                    state="verified",
                    ref_count=0,
                    verified_at=NOW,
                    updated_at=NOW,
                ),
                NodeArtifact(
                    id=_uuid(60_002),
                    node_id=node_b,
                    kind="model",
                    digest="4" * 64,
                    source=f"{artifact['repository']}@{artifact['revision']}",
                    size_bytes=100,
                    state="verified",
                    ref_count=0,
                    verified_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )

    recommendations = (
        _projection(sessions).detail(_uuid(26)).placement[0].recommendations
    )

    assert [item.node_ids for item in recommendations] == [[node_b], [node_a]]
    assert recommendations[0].score.artifact_reuse_bytes == 200
    assert recommendations[1].score.artifact_reuse_bytes == 0


def test_unresolved_revision_does_not_expose_an_invalid_preview_target() -> None:
    _engine, sessions = _database()
    document = _document(family="draft-family", title="Draft recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            27,
            slug="draft-recipe",
            title="Draft catalog title",
            document=document,
            lifecycle="draft",
        )
        _node(session, 1)

    detail = _projection(sessions).detail(_uuid(27))

    assert [reason.code for reason in detail.reasons] == ["recipe.unresolved"]
    placement = detail.placement[0]
    assert placement.recommendations == []
    assert len(placement.rejected_groups) == 1
    assert placement.rejected_groups[0].preview_targets == []
    assert "mapping.recipe_unresolved" in {
        reason.code for reason in placement.rejected_groups[0].reasons
    }


def test_rejected_node_and_group_evidence_preserves_capacity_and_fabric_reasons() -> (
    None
):
    _engine, sessions = _database()
    document = _document(family="evidence", nodes=2, title="Evidence recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            30,
            slug="evidence-recipe",
            title="Evidence catalog title",
            document=document,
        )
        node_a = _node(session, 1, fabric_address="10.0.0.1")
        node_b = _node(session, 2, fabric_address="10.0.0.1")
        node_c = _node(
            session,
            3,
            disk_free=100,
            memory_free=100,
            capabilities=["runtime.vonk.v1", "fabric.full_mesh.mbps.100"],
        )
        delayed = _node(session, 4, telemetry_age=7)
        stale = _node(session, 5, telemetry_age=21)
        missing = _node(session, 6, telemetry_age=None)
        stale_inventory = _node(session, 7, inventory_age=301)
        missing_inventory = _node(session, 8, inventory_age=None)
        session.add_all(
            [
                ResourceReservation(
                    id=_uuid(50_001),
                    node_id=node_c,
                    kind="disk",
                    resource_key="disk",
                    amount_bytes=60,
                    owner_kind="recipe-install",
                    owner_id=_uuid(50_101),
                    state="active",
                    plan_digest="1" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id=_uuid(50_002),
                    node_id=node_c,
                    kind="unified-memory",
                    resource_key="memory",
                    amount_bytes=60,
                    owner_kind="recipe-run",
                    owner_id=_uuid(50_102),
                    state="active",
                    plan_digest="2" * 64,
                    created_at=NOW,
                ),
            ]
        )

    placement = _projection(sessions).detail(_uuid(30)).placement[0]
    rejected_node_codes = {
        reason.code for node in placement.rejected_nodes for reason in node.reasons
    }
    rejected_group_codes = {
        reason.code for group in placement.rejected_groups for reason in group.reasons
    }

    assert {delayed, stale, missing, stale_inventory, missing_inventory} <= {
        node.node_id for node in placement.rejected_nodes
    }
    assert {
        "telemetry.delayed",
        "telemetry.stale",
        "telemetry.missing",
        "inventory.stale",
        "inventory.missing",
    } <= rejected_node_codes
    assert {
        "run.fabric_address_duplicate",
        "topology.fabric_insufficient",
        "reservation.disk",
        "reservation.memory",
        "install.insufficient_disk",
        "run.insufficient_memory",
    } <= rejected_group_codes
    assert all(group.group_complete for group in placement.rejected_groups)
    assert all(len(group.node_ids) == 2 for group in placement.rejected_groups)
    assert placement.rejected_evidence_truncated is False
    assert {node_a, node_b, node_c} == set(placement.candidate_node_ids)


def test_placement_bounds_are_observable_and_unsupported_topologies_do_not_search() -> (
    None
):
    _engine, sessions = _database()
    search_document = _document(family="bounded", nodes=3, title="Bounded recipe")
    unsupported = _document(family="unsupported", nodes=33, title="Wide recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            40,
            slug="bounded-recipe",
            title="Bounded catalog title",
            document=search_document,
        )
        _recipe(
            session,
            41,
            slug="wide-recipe",
            title="Wide catalog title",
            document=unsupported,
        )
        for value in range(1, 17):
            _node(session, value)

    bounded = _projection(sessions).detail(_uuid(40)).placement[0]
    assert bounded.limits.model_dump() == {
        "candidate_node_limit": 32,
        "examined_group_limit": 512,
        "recommendation_limit": 16,
        "rejected_node_evidence_limit": 32,
        "rejected_group_evidence_limit": 16,
        "artifact_evidence_per_node_limit": 512,
        "operational_row_evidence_limit": 512,
        "operational_member_evidence_limit": 16_384,
    }
    assert bounded.evaluated_group_count == 512
    assert bounded.search_complete is False
    assert len(bounded.recommendations) == 16
    assert all(group.group_complete for group in bounded.recommendations)
    assert "placement.search_truncated" in {reason.code for reason in bounded.reasons}

    wide = _projection(sessions).detail(_uuid(41)).placement[0]
    assert wide.node_count == 33
    assert wide.recommendations == []
    assert wide.rejected_groups == []
    assert wide.evaluated_group_count == 0
    assert wide.search_complete is False
    assert [reason.code for reason in wide.reasons] == [
        "topology.node_count_unsupported"
    ]


def test_detail_uses_a_fixed_set_query_count_instead_of_candidate_services() -> None:
    engine, sessions = _database()
    document = _document(family="query-bound", nodes=2, title="Query bound")
    with sessions.begin() as session:
        _recipe(
            session,
            50,
            slug="query-bound",
            title="Query bound catalog title",
            document=document,
        )
        _node(session, 1)
        _node(session, 2)

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.split()).lower()
        if normalized.startswith("select"):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", record)
    _projection(sessions).detail(_uuid(50))
    first = list(statements)
    statements.clear()
    event.remove(engine, "before_cursor_execute", record)

    with sessions.begin() as session:
        for value in range(3, 13):
            _node(session, value)

    event.listen(engine, "before_cursor_execute", record)
    _projection(sessions).detail(_uuid(50))
    second = list(statements)
    event.remove(engine, "before_cursor_execute", record)

    assert len(first) == len(second) == 21
    for table in (
        "local_recipes",
        "cluster_mappings",
        "cluster_mapping_nodes",
        "recipe_builds",
        "recipe_installations",
        "installation_nodes",
        "recipe_runs",
        "run_nodes",
        "agent_nodes",
        "node_inventory_snapshots",
        "node_telemetry_latest",
        "resource_reservations",
        "node_artifacts",
    ):
        assert sum(table in statement for statement in second) >= 1
    assert sum("node_artifacts" in statement for statement in second) == 1
    assert sum("resource_reservations" in statement for statement in second) == 1


def test_valid_long_v1_visual_fields_are_bounded_without_profile_projection() -> None:
    _engine, sessions = _database()
    document = _document(family="bounded-visual", title="Preserved visual title")
    document["metadata"]["description"] = "d" * 4_000
    document["artifacts"][0]["repository"] = "r" * 512
    validate_recipe(document)
    with sessions.begin() as session:
        recipe, _revision = _recipe(
            session,
            60,
            slug="bounded-visual",
            title="Preserved catalog title",
            document=document,
        )
        recipe.description = "c" * 6_000

    projection = _projection(sessions)
    root = projection.list()
    summary = root.models[0].recipes[0]
    detail = projection.detail(_uuid(60))

    assert summary.title == "Preserved catalog title"
    assert summary.description == "c" * 4_096
    assert summary.capabilities == ["openai"]
    assert summary.topology_name == "solo"
    assert detail.recipe.title == "Preserved catalog title"
    assert detail.recipe.description == "c" * 4_096
    assert detail.visual_recipe.metadata.title == "Preserved visual title"
    assert detail.visual_recipe.model.slug == "bounded-visual"
    assert detail.visual_recipe.metadata.description == "d" * 512
    assert detail.visual_recipe.artifacts[0].repository == "r" * 256
    assert detail.topology.name == "solo"
    assert detail.placement[0].topology_name == "solo"
    assert "recipe.visual_text_truncated" in {item.code for item in detail.reasons}


def test_visual_projection_keeps_non_openai_interface_path_without_endpoint_fields() -> None:
    """Break caught: job interfaces fail visual projection without an OpenAI port."""

    _engine, sessions = _database()
    document = _document(family="image-job", title="Image job")
    document["interfaces"] = [{"adapter": "image-job", "path": "/generate"}]
    document["validation"]["validators"] = [
        {"interface": "image-job", "checks": ["job.completed"]}
    ]
    validate_recipe(document)
    with sessions.begin() as session:
        _recipe(
            session,
            601,
            slug="image-job",
            title="Image job catalog title",
            document=document,
        )

    interface = _projection(sessions).detail(_uuid(601)).visual_recipe.interfaces[0]

    assert interface.adapter == "image-job"
    assert interface.path == "/generate"
    assert interface.port is None
    assert interface.model_aliases == []
    assert interface.health_path is None


@pytest.mark.parametrize("rank_evidence", ["stale", "missing", "failed"])
def test_nonterminal_run_with_inexact_rank_evidence_is_loaded_but_degraded(
    rank_evidence: str,
) -> None:
    _engine, sessions = _database()
    document = _document(family="degraded-run", nodes=2, title="Degraded run")
    with sessions.begin() as session:
        _recipe(
            session,
            61,
            slug="degraded-run",
            title="Degraded run catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_611))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=61,
            installed=True,
            running=True,
        )
        session.flush()
        ranks = list(
            session.query(RunNode)
            .filter(RunNode.run_id == identifiers["run_id"])
            .order_by(RunNode.rank)
        )
        if rank_evidence == "stale":
            ranks[0].updated_at = NOW - timedelta(seconds=301)
        elif rank_evidence == "missing":
            session.delete(ranks[0])
        else:
            ranks[0].state = "failed"

    recommendation = (
        _projection(sessions).detail(_uuid(61)).placement[0].recommendations[0]
    )
    codes = {item.code for item in recommendation.reasons}

    assert recommendation.load_state == "loaded"
    assert recommendation.run_ids == [identifiers["run_id"]]
    assert {"run.loaded", "run.degraded"} <= codes


def test_uninstalled_installation_is_not_present_and_has_no_run_preview() -> None:
    _engine, sessions = _database()
    document = _document(family="uninstalled", title="Uninstalled recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            62,
            slug="uninstalled",
            title="Uninstalled catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_621))
        node_id = _node(session, 1)
        identifiers = _operational_group(
            session, revision, (node_id,), value=62, installed=True
        )
        session.flush()
        installation = session.get(RecipeInstallation, identifiers["installation_id"])
        installation.state = "uninstalled"
        for node in session.query(InstallationNode).filter_by(
            installation_id=installation.id
        ):
            node.state = "uninstalled"

    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0]
    recommendation = projection.detail(_uuid(62)).placement[0].recommendations[0]

    assert summary.installations == []
    assert summary.installation_total_count == 0
    assert recommendation.install_state == "not_present"
    assert recommendation.installation_ids == []
    assert [item.kind for item in recommendation.preview_targets] == [
        "mapping",
        "install",
    ]


def test_worst_case_group_reasons_are_deduplicated_and_bounded() -> None:
    _engine, sessions = _database()
    document = _document(family="reason-bound", nodes=32, title="Reason bound")
    with sessions.begin() as session:
        _recipe(
            session,
            63,
            slug="reason-bound",
            title="Reason bound catalog title",
            document=document,
        )
        for value in range(1, 33):
            node_id = _node(session, value, disk_free=100, memory_free=100)
            session.add_all(
                [
                    ResourceReservation(
                        id=_uuid(70_000 + value),
                        node_id=node_id,
                        kind="disk",
                        resource_key="disk",
                        amount_bytes=60,
                        owner_kind="recipe-install",
                        owner_id=_uuid(71_000 + value),
                        state="active",
                        plan_digest=f"{value:064x}",
                        created_at=NOW,
                    ),
                    ResourceReservation(
                        id=_uuid(72_000 + value),
                        node_id=node_id,
                        kind="unified-memory",
                        resource_key="memory",
                        amount_bytes=60,
                        owner_kind="recipe-run",
                        owner_id=_uuid(73_000 + value),
                        state="active",
                        plan_digest=f"{value + 100:064x}",
                        created_at=NOW,
                    ),
                ]
            )

    rejected = _projection(sessions).detail(_uuid(63)).placement[0].rejected_groups[0]
    reasons = rejected.reasons
    severity_order = {"error": 0, "warning": 1, "info": 2}
    keys = [(severity_order[item.severity], item.code, item.detail) for item in reasons]

    assert len(reasons) == 64
    assert len(set(keys)) == 64
    assert keys == sorted(keys)
    assert "projection.reasons_truncated" in {item.code for item in reasons}
    assert {
        "action.preview_required",
        "install.insufficient_disk",
        "install.not_present",
        "reservation.disk",
        "reservation.memory",
        "run.insufficient_memory",
    } <= {item.code for item in reasons}


def test_active_run_lineage_precedes_512_newer_operational_rows_without_more_queries() -> (
    None
):
    engine, sessions = _database()
    document = _document(family="active-lineage", title="Active lineage")
    with sessions.begin() as session:
        _recipe(
            session,
            64,
            slug="active-lineage",
            title="Active lineage catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_641))
        node_id = _node(session, 1)
        active = _operational_group(
            session,
            revision,
            (node_id,),
            value=64,
            installed=True,
            running=True,
        )
        for value in range(512):
            mapping_id = _uuid(80_000 + value)
            build_id = _uuid(81_000 + value)
            installation_id = _uuid(82_000 + value)
            updated_at = NOW - timedelta(microseconds=value + 1)
            session.add_all(
                [
                    ClusterMapping(
                        id=mapping_id,
                        recipe_revision_id=revision.id,
                        topology_name="single",
                        generation=1,
                        node_count=1,
                        state="stale",
                        parameters={},
                        placement_digest=f"{100_000 + value:064x}",
                        endpoint_owner_node_id=node_id,
                        created_by="operator",
                        created_at=updated_at,
                        updated_at=updated_at,
                    ),
                    RecipeBuild(
                        id=build_id,
                        recipe_revision_id=revision.id,
                        builder_node_id=node_id,
                        source_bundle_sha256="a" * 64,
                        build_input_sha256=f"{110_000 + value:064x}",
                        state="failed",
                        policy_report={},
                        plan={},
                        image_digest=None,
                        oci_layout_sha256=None,
                        image_bytes=None,
                        error="historical failure",
                        created_at=updated_at,
                        updated_at=updated_at,
                    ),
                    RecipeInstallation(
                        id=installation_id,
                        recipe_revision_id=revision.id,
                        mapping_id=mapping_id,
                        mapping_generation=1,
                        recipe_build_id=build_id,
                        image_digest="sha256:" + "b" * 64,
                        plan_digest=f"{120_000 + value:064x}",
                        plan={},
                        state="uninstalled",
                        actor="operator",
                        created_at=updated_at,
                        updated_at=updated_at,
                    ),
                ]
            )

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if " ".join(statement.split()).lower().startswith("select"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    detail = _projection(sessions).detail(_uuid(64))
    event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 21
    assert active["run_id"] in {item.run_id for item in detail.operational_state.runs}
    assert active["installation_id"] in {
        item.installation_id for item in detail.operational_state.installations
    }
    assert active["mapping_id"] in {
        item.mapping_id for item in detail.operational_state.mappings
    }
    assert active["recipe_build_id"] in {
        item.recipe_build_id for item in detail.operational_state.builds
    }
    reason = next(
        item
        for item in detail.reasons
        if item.code == "recipe.operational_state_truncated"
    )
    assert "installations: at least 513 total/512 returned" in reason.detail


def test_unrelated_active_workload_prefers_empty_node_without_claiming_exact_load() -> (
    None
):
    _engine, sessions = _database()
    target_document = _document(family="target", title="Target recipe")
    occupied_document = _document(family="occupied", title="Occupied recipe")
    with sessions.begin() as session:
        _recipe(
            session,
            70,
            slug="target-recipe",
            title="Target catalog title",
            document=target_document,
        )
        _recipe(
            session,
            71,
            slug="occupied-recipe",
            title="Occupied catalog title",
            document=occupied_document,
        )
        occupied_revision = session.get(LocalRecipeRevision, _uuid(1_711))
        occupied_node = _node(session, 1)
        empty_node = _node(session, 2)
        unrelated = _operational_group(
            session,
            occupied_revision,
            (occupied_node,),
            value=70,
            installed=True,
            running=True,
        )

    placement = _projection(sessions).detail(_uuid(70)).placement[0]

    assert [item.node_ids for item in placement.recommendations] == [
        [empty_node],
        [occupied_node],
    ]
    empty, occupied = placement.recommendations
    assert empty.score.active_run_count == 0
    assert occupied.score.active_run_count == 1
    assert occupied.load_state == "not_loaded"
    assert occupied.run_ids == []
    occupied_reasons = {item.code: item.detail for item in occupied.reasons}
    assert "run.loaded" in occupied_reasons
    assert unrelated["run_id"] not in " ".join(occupied_reasons.values())
    assert "unrelated" in occupied_reasons["run.loaded"].lower()
    for recommendation in (empty, occupied):
        assert {
            "action.preview_required",
            "placement.single_group_preview_required",
        } <= {item.code for item in recommendation.reasons}


def test_active_mapping_precedes_newer_ready_historical_mapping_for_same_group() -> (
    None
):
    _engine, sessions = _database()
    document = _document(family="mapping-priority", title="Mapping priority")
    with sessions.begin() as session:
        _recipe(
            session,
            72,
            slug="mapping-priority",
            title="Mapping priority catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_721))
        node_id = _node(session, 1)
        active = _operational_group(
            session,
            revision,
            (node_id,),
            value=72,
            installed=True,
            running=True,
        )
        newer_mapping = ClusterMapping(
            id=_uuid(90_000),
            recipe_revision_id=revision.id,
            topology_name="solo",
            generation=2,
            node_count=1,
            state="ready",
            parameters={},
            placement_digest="9" * 64,
            endpoint_owner_node_id=node_id,
            created_by="operator",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(newer_mapping)
        session.flush()
        session.add(
            ClusterMappingNode(
                id=_uuid(90_001),
                mapping_id=newer_mapping.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        historical_build = RecipeBuild(
            id=_uuid(90_002),
            recipe_revision_id=revision.id,
            builder_node_id=node_id,
            source_bundle_sha256="a" * 64,
            build_input_sha256="8" * 64,
            state="failed",
            policy_report={},
            plan={},
            image_digest=None,
            oci_layout_sha256=None,
            image_bytes=None,
            error="historical",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(historical_build)
        session.flush()
        session.add(
            RecipeInstallation(
                id=_uuid(90_003),
                recipe_revision_id=revision.id,
                mapping_id=newer_mapping.id,
                mapping_generation=2,
                recipe_build_id=historical_build.id,
                image_digest="sha256:" + "7" * 64,
                plan_digest="7" * 64,
                plan={},
                state="uninstalled",
                actor="operator",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    recommendation = (
        _projection(sessions).detail(_uuid(72)).placement[0].recommendations[0]
    )

    assert recommendation.mapping_id == active["mapping_id"]
    assert recommendation.installation_ids == [active["installation_id"]]
    assert recommendation.run_ids == [active["run_id"]]


def test_grouped_reservations_do_not_hide_later_candidate_port_conflict() -> None:
    _engine, sessions = _database()
    document = _document(family="reservation-bound", title="Reservation bound")
    endpoint_port = str(document["interfaces"][0]["port"])
    with sessions.begin() as session:
        _recipe(
            session,
            73,
            slug="reservation-bound",
            title="Reservation bound catalog title",
            document=document,
        )
        noisy_node = _node(session, 1, disk_free=2_000)
        conflicted_node = _node(session, 2)
        session.add_all(
            [
                ResourceReservation(
                    id=_uuid(100_000 + value),
                    node_id=noisy_node,
                    kind="disk",
                    resource_key=f"disk-{value}",
                    amount_bytes=1,
                    owner_kind="recipe-install",
                    owner_id=_uuid(200_000 + value),
                    state="active",
                    plan_digest=f"{300_000 + value:064x}",
                    created_at=NOW,
                )
                for value in range(16_384)
            ]
        )
        session.add(
            ResourceReservation(
                id=_uuid(500_000),
                node_id=conflicted_node,
                kind="port",
                resource_key=endpoint_port,
                amount_bytes=1,
                owner_kind="recipe-run",
                owner_id=_uuid(500_001),
                state="active",
                plan_digest="5" * 64,
                created_at=NOW,
            )
        )

    placement = _projection(sessions).detail(_uuid(73)).placement[0]
    by_node = {
        item.node_ids[0]: item
        for item in [*placement.recommendations, *placement.rejected_groups]
    }

    assert "run.port_occupied" in {
        item.code for item in by_node[conflicted_node].reasons
    }
    assert by_node[conflicted_node].eligible is False


def test_huge_topology_role_count_is_projected_with_constant_bounded_memory() -> None:
    _engine, sessions = _database()
    document = _document(family="huge-profile", nodes=2, title="Huge profile")
    topology = document["topology"]
    topology["node_count"] = 1_000_000
    topology["roles"][1]["count"] = 999_999
    topology["parallelism"]["tensor"] = 1_000_000
    validate_recipe(document)
    with sessions.begin() as session:
        _recipe(
            session,
            74,
            slug="huge-profile",
            title="Huge profile catalog title",
            document=document,
        )

    tracemalloc.start()
    summary = _projection(sessions).list().models[0].recipes[0]
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert summary.topology_name == "pair"
    assert _projection(sessions).detail(_uuid(74)).topology.node_count == 1_000_000
    assert peak < 4_000_000


@pytest.mark.parametrize("endpoint_port", [8_000, 29_500])
def test_rendezvous_checks_declared_endpoint_owner_and_runtime_port(
    endpoint_port: int,
) -> None:
    _engine, sessions = _database()
    document = _document(family="rendezvous", nodes=2, title="Rendezvous")
    document["interfaces"][0]["port"] = endpoint_port
    document["topology"]["roles"][0]["endpoint_owner"] = False
    document["topology"]["roles"][1]["endpoint_owner"] = True
    validate_recipe(document)
    with sessions.begin() as session:
        _recipe(
            session,
            75,
            slug=f"rendezvous-{endpoint_port}",
            title="Rendezvous catalog title",
            document=document,
        )
        rank_zero = _node(session, 1)
        endpoint_owner = _node(session, 2)
        if endpoint_port != 29_500:
            session.add(
                ResourceReservation(
                    id=_uuid(600_000),
                    node_id=endpoint_owner,
                    kind="port",
                    resource_key="29500",
                    amount_bytes=1,
                    owner_kind="recipe-run",
                    owner_id=_uuid(600_001),
                    state="active",
                    plan_digest="6" * 64,
                    created_at=NOW,
                )
            )

    placement = _projection(sessions).detail(_uuid(75)).placement[0]
    group = [*placement.recommendations, *placement.rejected_groups][0]
    reasons = {item.code for item in group.reasons}

    assert group.node_ids == [rank_zero, endpoint_owner]
    assert group.eligible is False
    assert "run.rendezvous_port_occupied" in reasons


def test_recommendation_reasons_explain_every_stable_preference_dimension() -> None:
    _engine, sessions = _database()
    document = _document(family="preference-reasons", title="Preferences")
    with sessions.begin() as session:
        _recipe(
            session,
            76,
            slug="preference-reasons",
            title="Preference catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_761))
        node_id = _node(session, 1)
        _operational_group(
            session,
            revision,
            (node_id,),
            value=76,
            installed=True,
        )

    recommendation = (
        _projection(sessions).detail(_uuid(76)).placement[0].recommendations[0]
    )
    codes = {item.code for item in recommendation.reasons}

    assert {
        "install.complete",
        "preference.artifact_reuse",
        "preference.disk_headroom",
        "preference.memory_headroom",
        "preference.telemetry_freshness",
        "preference.node_empty",
    } <= codes


def test_schema_valid_oversized_recipe_numbers_are_saturated_with_a_reason() -> None:
    _engine, sessions = _database()
    huge = 1 << 80
    maximum = 9_223_372_036_854_775_807
    document = _document(family="numeric-bounds", nodes=2, title="Numeric bounds")
    topology = document["topology"]
    topology["node_count"] = huge + 1
    topology["roles"][1]["count"] = huge
    topology["parallelism"]["tensor"] = huge + 1
    topology["fabric"]["minimum_bandwidth_mbps"] = huge
    for role in topology["roles"]:
        for field in role["resources"]["disk"]:
            role["resources"]["disk"][field] = huge
        for field in role["resources"]["memory"]:
            if field != "kind":
                role["resources"]["memory"][field] = huge
    document["artifacts"][0]["download_bytes"] = huge
    document["artifacts"][0]["installed_bytes"] = huge
    document["build"]["context"]["expected_bytes"] = huge
    document["build"]["resources"]["download_bytes"] = huge
    document["build"]["resources"]["temporary_bytes"] = huge
    document["build"]["resources"]["memory_bytes"] = huge
    validate_recipe(document)
    with sessions.begin() as session:
        _recipe(
            session,
            77,
            slug="numeric-bounds",
            title="Numeric bounds catalog title",
            document=document,
        )

    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0]
    detail = projection.detail(_uuid(77))

    assert summary.topology_name == "pair"
    assert detail.topology.node_count == maximum
    assert detail.topology.roles[1].count == maximum
    assert detail.visual_recipe.artifacts[0].installed_bytes == maximum
    assert {
        "topology.node_count_unsupported",
        "projection.numeric_truncated",
    } <= {item.code for item in detail.placement[0].reasons}
    for reasons in (summary.reasons, detail.reasons):
        assert "recipe.numeric_truncated" in {item.code for item in reasons}


@pytest.mark.parametrize("rank_evidence", ["missing", "boundary"])
def test_root_run_health_uses_mapping_count_and_strict_freshness_boundary(
    rank_evidence: str,
) -> None:
    _engine, sessions = _database()
    document = _document(family="root-health", nodes=2, title="Root health")
    with sessions.begin() as session:
        _recipe(
            session,
            78,
            slug=f"root-health-{rank_evidence}",
            title="Root health catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_781))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=78,
            installed=True,
            running=True,
        )
        session.flush()
        if rank_evidence == "missing":
            session.get(ClusterMapping, identifiers["mapping_id"]).state = "stale"
            session.flush()
            mapping_rank = (
                session.query(ClusterMappingNode)
                .filter_by(mapping_id=identifiers["mapping_id"], rank=1)
                .one()
            )
            run_rank = (
                session.query(RunNode)
                .filter_by(run_id=identifiers["run_id"], rank=1)
                .one()
            )
            session.delete(mapping_rank)
            session.delete(run_rank)
        else:
            session.query(RunNode).filter_by(
                run_id=identifiers["run_id"], rank=0
            ).one().updated_at = NOW - timedelta(seconds=300)

    summary = _projection(sessions).list().models[0].recipes[0].runs[0]

    assert summary.expected_rank_count == 2
    assert summary.healthy is False
    assert summary.healthy_rank_count == 1


def test_complete_installation_precedes_newer_partial_mapping_and_capped_history() -> (
    None
):
    _engine, sessions = _database()
    document = _document(family="complete-priority", title="Complete priority")
    with sessions.begin() as session:
        _recipe(
            session,
            79,
            slug="complete-priority",
            title="Complete priority catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_791))
        node_id = _node(session, 1)
        complete = _operational_group(
            session,
            revision,
            (node_id,),
            value=79,
            installed=True,
        )
        newer_partial = _operational_group(
            session,
            revision,
            (node_id,),
            value=80,
            installed=False,
        )
        session.flush()
        session.get(ClusterMapping, newer_partial["mapping_id"]).updated_at = NOW
        session.get(
            RecipeInstallation, newer_partial["installation_id"]
        ).updated_at = NOW
        base_installation = session.get(RecipeInstallation, complete["installation_id"])
        for value in range(17):
            installation_id = _uuid(700_000 + value)
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=revision.id,
                    mapping_id=complete["mapping_id"],
                    mapping_generation=1,
                    recipe_build_id=complete["recipe_build_id"],
                    image_digest=base_installation.image_digest,
                    plan_digest=f"{710_000 + value:064x}",
                    plan={},
                    state="partial",
                    actor="operator",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    recommendation = (
        _projection(sessions).detail(_uuid(79)).placement[0].recommendations[0]
    )

    assert recommendation.mapping_id == complete["mapping_id"]
    assert recommendation.install_state == "complete"
    assert complete["installation_id"] in recommendation.installation_ids
    assert any(item.kind == "run" for item in recommendation.preview_targets)


def test_per_node_artifact_evidence_truncation_is_observable() -> None:
    _engine, sessions = _database()
    document = _document(family="artifact-bound", title="Artifact bound")
    with sessions.begin() as session:
        _recipe(
            session,
            81,
            slug="artifact-bound",
            title="Artifact bound catalog title",
            document=document,
        )
        node_id = _node(session, 1)
        session.add_all(
            [
                NodeArtifact(
                    id=_uuid(800_000 + value),
                    node_id=node_id,
                    kind="auxiliary",
                    digest=f"{900_000 + value:064x}",
                    source=f"evidence-{value}",
                    size_bytes=1,
                    state="verified",
                    ref_count=0,
                    verified_at=NOW,
                    updated_at=NOW,
                )
                for value in range(513)
            ]
        )

    placement = _projection(sessions).detail(_uuid(81)).placement[0]

    assert placement.search_complete is False
    assert placement.limits.artifact_evidence_per_node_limit == 512
    assert "projection.evidence_truncated" in {item.code for item in placement.reasons}
    assert placement.recommendations == []
    rejected = next(
        item for item in placement.rejected_groups if item.node_ids == [node_id]
    )
    assert rejected.eligible is False
    assert "projection.evidence_truncated" in {item.code for item in rejected.reasons}


@pytest.mark.parametrize("rank_evidence", ["duplicate", "missing"])
def test_installation_coverage_matches_run_admission_without_byte_thresholds(
    rank_evidence: str,
) -> None:
    _engine, sessions = _database()
    document = _document(family="install-parity", nodes=2, title="Install parity")
    with sessions.begin() as session:
        _recipe(
            session,
            82,
            slug=f"install-parity-{rank_evidence}",
            title="Install parity catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_821))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=82,
            installed=True,
        )
        installation_nodes = list(
            session.query(InstallationNode)
            .filter_by(installation_id=identifiers["installation_id"])
            .order_by(InstallationNode.rank)
        )
        for node in installation_nodes:
            node.installed_bytes = 1
        if rank_evidence == "duplicate":
            installation_nodes[1].rank = 0
        else:
            session.delete(installation_nodes[1])

    admission = RunAdmissionService(
        sessions,
        inventory_max_age=300,
        memory_floor_bytes=50,
    ).plan_run(identifiers["installation_id"], "model", now=NOW)
    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0].installations[0]
    recommendation = projection.detail(_uuid(82)).placement[0].recommendations[0]
    has_run_target = any(
        target.kind == "run" for target in recommendation.preview_targets
    )

    assert admission.allowed is False
    assert summary.complete is False
    assert recommendation.install_state != "complete"
    assert has_run_target is admission.allowed


def test_low_install_byte_evidence_matches_run_admission_and_stages_run() -> None:
    _engine, sessions = _database()
    document = _document(family="low-byte-parity", nodes=2, title="Low bytes")
    with sessions.begin() as session:
        _recipe(
            session,
            83,
            slug="low-byte-parity",
            title="Low byte catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_831))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=83,
            installed=True,
        )
        for node in session.query(InstallationNode).filter_by(
            installation_id=identifiers["installation_id"]
        ):
            node.installed_bytes = 1

    admission = RunAdmissionService(
        sessions,
        inventory_max_age=300,
        memory_floor_bytes=50,
    ).plan_run(identifiers["installation_id"], "model", now=NOW)
    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0].installations[0]
    recommendation = projection.detail(_uuid(83)).placement[0].recommendations[0]

    assert admission.allowed is True
    assert summary.complete is True
    assert summary.installed_rank_count == 2
    assert recommendation.install_state == "complete"
    assert any(target.kind == "run" for target in recommendation.preview_targets)


@pytest.mark.parametrize(
    ("aggregate_state", "route_state", "route_reason"),
    [
        ("starting", "pending", "run.route_pending"),
        ("stopping", "failed", "run.route_failed"),
    ],
)
def test_rank_health_matches_run_status_independently_of_run_and_route_state(
    aggregate_state: str,
    route_state: str,
    route_reason: str,
) -> None:
    _engine, sessions = _database()
    document = _document(family="run-health-parity", nodes=2, title="Run health")
    with sessions.begin() as session:
        _recipe(
            session,
            84,
            slug=f"run-health-{route_state}",
            title="Run health catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_841))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=84,
            installed=True,
            running=True,
        )
        run = session.get(RecipeRun, identifiers["run_id"])
        run.state = aggregate_state
        run.route_state = route_state

    authoritative = _run_status_service(sessions).run_status(identifiers["run_id"])
    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0].runs[0]
    recommendation = projection.detail(_uuid(84)).placement[0].recommendations[0]
    reason_codes = {reason.code for reason in recommendation.reasons}

    assert authoritative.healthy is True
    assert summary.healthy is authoritative.healthy
    assert summary.state == aggregate_state
    assert summary.route_state == route_state
    assert "run.degraded" not in reason_codes
    assert route_reason in reason_codes


def test_more_than_512_active_exact_runs_fail_closed_for_placement_inputs() -> None:
    _engine, sessions = _database()
    document = _document(family="current-evidence-cap", title="Evidence cap")
    with sessions.begin() as session:
        _recipe(
            session,
            85,
            slug="current-evidence-cap",
            title="Evidence cap catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_851))
        node_id = _node(session, 1)
        identifiers = _operational_group(
            session,
            revision,
            (node_id,),
            value=85,
            installed=True,
            running=True,
        )
        base_run = session.get(RecipeRun, identifiers["run_id"])
        for value in range(512):
            run_id = _uuid(1_000_000 + value)
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=identifiers["installation_id"],
                    mapping_id=identifiers["mapping_id"],
                    mapping_generation=1,
                    alias=f"active-{value}",
                    plan_digest=f"{1_100_000 + value:064x}",
                    plan=deepcopy(base_run.plan),
                    state="running",
                    route_state="published",
                    actor="operator",
                    created_at=NOW - timedelta(seconds=30),
                    updated_at=NOW - timedelta(seconds=2),
                )
            )
            session.flush()
            session.add(
                RunNode(
                    id=_uuid(1_200_000 + value),
                    run_id=run_id,
                    node_id=node_id,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8_000,
                    reserved_memory_bytes=120,
                    observed_memory_bytes=80,
                    endpoint=None,
                    evidence_digest="e" * 64,
                    updated_at=NOW - timedelta(seconds=2),
                )
            )

    placement = _projection(sessions).detail(_uuid(85)).placement[0]
    group = placement.rejected_groups[0]
    codes = {reason.code for reason in group.reasons}

    assert placement.search_complete is False
    assert placement.limits.operational_row_evidence_limit == 512
    assert placement.limits.operational_member_evidence_limit == 16_384
    assert placement.evidence_counts.runs == 513
    assert placement.evidence_counts.truncated_collections == ["runs"]
    assert placement.recommendations == []
    assert group.eligible is False
    assert group.install_state == "unknown"
    assert group.load_state == "unknown"
    assert group.installation_ids == []
    assert group.run_ids == []
    assert {target.kind for target in group.preview_targets} == {"mapping"}
    assert "projection.evidence_truncated" in codes
    assert "install.complete" not in codes
    assert "run.loaded" not in codes


@pytest.mark.parametrize("mapping_fault", ["stale_generation", "not_ready"])
def test_installation_coverage_matches_run_admission_mapping_authority(
    mapping_fault: str,
) -> None:
    _engine, sessions = _database()
    document = _document(
        family="install-mapping-parity", nodes=2, title="Mapping parity"
    )
    with sessions.begin() as session:
        _recipe(
            session,
            88,
            slug=f"install-mapping-{mapping_fault}",
            title="Install mapping parity",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_881))
        node_a = _node(session, 1)
        node_b = _node(session, 2)
        identifiers = _operational_group(
            session,
            revision,
            (node_a, node_b),
            value=88,
            installed=True,
        )
        mapping = session.get(ClusterMapping, identifiers["mapping_id"])
        if mapping_fault == "stale_generation":
            mapping.generation = 2
        else:
            mapping.state = "planned"

    with pytest.raises(
        ValueError, match="cluster mapping generation changed after installation"
    ):
        RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=50,
        ).plan_run(identifiers["installation_id"], "model", now=NOW)

    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0].installations[0]
    recommendation = projection.detail(_uuid(88)).placement[0].recommendations[0]

    assert summary.complete is False
    assert recommendation.install_state != "complete"
    assert not any(target.kind == "run" for target in recommendation.preview_targets)


def test_run_health_uses_immutable_plan_roles_like_run_status() -> None:
    _engine, sessions = _database()
    document = _document(family="run-plan-parity", nodes=2, title="Plan parity")
    with sessions.begin() as session:
        _recipe(
            session,
            89,
            slug="run-plan-role-parity",
            title="Run plan role parity",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_891))
        identifiers = _operational_group(
            session,
            revision,
            (_node(session, 1), _node(session, 2)),
            value=89,
            installed=True,
            running=True,
        )
        run = session.get(RecipeRun, identifiers["run_id"])
        plan = deepcopy(run.plan)
        plan["nodes"][1]["role"] = "changed-after-start"
        run.plan = plan

    authoritative = _run_status_service(sessions).run_status(identifiers["run_id"])
    projection = _projection(sessions)
    summary = projection.list().models[0].recipes[0].runs[0]
    recommendation = projection.detail(_uuid(89)).placement[0].recommendations[0]

    assert authoritative.healthy is False
    assert summary.healthy is authoritative.healthy
    assert "run.degraded" in {reason.code for reason in recommendation.reasons}


@pytest.mark.parametrize(
    ("plan_nodes", "evidence_code"),
    [
        (None, "run.plan_invalid"),
        (["not-a-member"], "run.plan_invalid"),
        (
            [
                {"node_id": _node_id(1), "rank": 0, "role": "entrypoint"},
                {"node_id": _node_id(1), "rank": 0, "role": "entrypoint"},
            ],
            "run.plan_invalid",
        ),
        (
            [
                {"node_id": _node_id(value + 1), "rank": value, "role": "worker"}
                for value in range(33)
            ],
            "projection.evidence_truncated",
        ),
    ],
)
def test_malformed_or_oversized_run_plan_fails_closed_with_explicit_evidence(
    plan_nodes: object,
    evidence_code: str,
) -> None:
    _engine, sessions = _database()
    document = _document(family="run-plan-bounds", title="Plan bounds")
    with sessions.begin() as session:
        _recipe(
            session,
            90,
            slug=f"run-plan-bounds-{evidence_code}",
            title="Run plan bounds",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_901))
        identifiers = _operational_group(
            session,
            revision,
            (_node(session, 1),),
            value=90,
            installed=True,
            running=True,
        )
        run = session.get(RecipeRun, identifiers["run_id"])
        run.plan = {"schema_version": 1, "nodes": plan_nodes}

    authoritative = _run_status_service(sessions).run_status(identifiers["run_id"])
    projection = _projection(sessions)
    recipe = projection.list().models[0].recipes[0]
    recommendation = projection.detail(_uuid(90)).placement[0].recommendations[0]

    assert authoritative.healthy is False
    assert recipe.runs[0].healthy is authoritative.healthy
    assert evidence_code in {reason.code for reason in recipe.reasons}
    assert evidence_code in {reason.code for reason in recommendation.reasons}
    assert "run.degraded" in {reason.code for reason in recommendation.reasons}


def test_degraded_pending_route_copy_does_not_assert_rank_health() -> None:
    _engine, sessions = _database()
    document = _document(family="route-copy", nodes=2, title="Route copy")
    with sessions.begin() as session:
        _recipe(
            session,
            91,
            slug="degraded-pending-route",
            title="Degraded pending route",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_911))
        identifiers = _operational_group(
            session,
            revision,
            (_node(session, 1), _node(session, 2)),
            value=91,
            installed=True,
            running=True,
        )
        run = session.get(RecipeRun, identifiers["run_id"])
        run.route_state = "pending"
        rank = session.query(RunNode).filter_by(run_id=run.id, rank=1).one()
        rank.state = "failed"

    recommendation = (
        _projection(sessions).detail(_uuid(91)).placement[0].recommendations[0]
    )
    reasons = {reason.code: reason.detail for reason in recommendation.reasons}

    assert "run.degraded" in reasons
    assert reasons["run.route_pending"] == (
        "Route publication is pending. Rank health is projected separately."
    )


def test_mapping_action_inputs_are_bound_to_the_recipe_topology() -> None:
    _engine, sessions = _database()
    document = _document(family="mapping-inputs", title="Mapping inputs")
    with sessions.begin() as session:
        _recipe(
            session,
            86,
            slug="mapping-inputs",
            title="Mapping input catalog title",
            document=document,
        )
        revision = session.get(LocalRecipeRevision, _uuid(1_861))
        node_id = _node(session, 1)

    detail = _projection(sessions).detail(_uuid(86))
    recommendation = detail.placement[0].recommendations[0]
    mapping_target = next(
        target for target in recommendation.preview_targets if target.kind == "mapping"
    )
    authoritative = ClusterMappingService(sessions).preview(
        revision.id,
        (node_id,),
        parameters=mapping_target.input.parameters,
        actor="operator",
    )

    assert mapping_target.input.parameters == {}
    assert authoritative.topology_name == "solo"
    assert authoritative.parameters == {"max_model_len": 32_768}


def test_family_spans_pages_with_signed_compound_cursor_binding() -> None:
    _engine, sessions = _database()
    with sessions.begin() as session:
        for value, slug in enumerate(("alpha-page", "bravo-page", "charlie-page"), 87):
            _recipe(
                session,
                value,
                slug=slug,
                title=f"Page recipe {value}",
                document=_document(family="paged-family", title=f"Page {value}"),
            )

    codec = TokenCodec(b"k" * 32).cursor_codec()
    projection = LibraryProjection(
        sessions,
        cursors=codec,
        clock=lambda: NOW,
        disk_floor_bytes=50,
        memory_floor_bytes=50,
    )
    first = projection.list(limit=1)
    second = projection.list(limit=1, cursor=first.next_cursor)
    third = projection.list(limit=1, cursor=second.next_cursor)

    assert [page.models[0].family for page in (first, second, third)] == [
        "paged-family",
        "paged-family",
        "paged-family",
    ]
    assert [page.models[0].recipes[0].slug for page in (first, second, third)] == [
        "alpha-page",
        "bravo-page",
        "charlie-page",
    ]
    assert codec.decode(
        first.next_cursor,
        resource="library-recipes",
        order="slug-asc/id-asc/v1",
        context={"limit": 1},
    ) == ["alpha-page", _uuid(87)]
    assert projection.list(limit=1).next_cursor == first.next_cursor
    with pytest.raises(ValueError, match="cursor"):
        projection.list(limit=2, cursor=first.next_cursor)
    with pytest.raises(ValueError, match="cursor"):
        projection.list(
            limit=1,
            cursor=first.next_cursor[:-1]
            + ("A" if first.next_cursor[-1] != "A" else "B"),
        )
