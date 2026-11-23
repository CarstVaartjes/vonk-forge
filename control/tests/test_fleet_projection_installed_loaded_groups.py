"""Fleet projection: installed loaded groups."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import Table, create_engine, literal, select, text, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.fleet_projection.common import (
    FleetSnapshot,
)
from vonk_control.fleet_projection.service import FleetProjection
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import (
    AgentNode,
    AgentNodeContact,
    AgentNodeProfile,
)
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.models.telemetry import (
    NodeTelemetryLatest,
)

from tests.observation_transfer_peer import observation_document

from .fleet_projection_support import (
    NODE_A,
    NODE_B,
    NOW,
    _canonical_catalog_documents,
    _certificate,
    _profile,
    _telemetry,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_hostname,
)

NON_RFC_BOOT_ID = "00000000-0000-0000-0000-000000000001"


def test_installed_and_loaded_groups_require_every_exact_current_rank(capsys) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000101"
    revision_id = "00000000-0000-4000-8000-000000000102"
    mapping_id = "00000000-0000-4000-8000-000000000103"
    build_id = "00000000-0000-4000-8000-000000000104"
    complete_installation_id = "00000000-0000-4000-8000-000000000105"
    partial_installation_id = "00000000-0000-4000-8000-000000000106"
    healthy_run_id = "00000000-0000-4000-8000-000000000107"
    degraded_run_id = "00000000-0000-4000-8000-000000000108"
    route_failed_run_id = "00000000-0000-4000-8000-000000000121"
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        NODE_A,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        NODE_B,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _certificate(NODE_A, "groups-a"),
                _certificate(NODE_B, "groups-b"),
            ]
        )
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            "00000000-0000-4000-8000-000000000305",
            "00000000-0000-4000-8000-000000000306",
            slug="pair-recipe",
            title="Pair Recipe",
        )
        mapping = ClusterMapping(
            id=mapping_id,
            recipe_revision_id=revision_id,
            topology_name="pair",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="2" * 64,
            endpoint_owner_node_id=NODE_A,
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        build = RecipeBuild(
            id=build_id,
            recipe_revision_id=revision_id,
            builder_node_id=NODE_A,
            source_bundle_sha256="3" * 64,
            build_input_sha256="4" * 64,
            state="succeeded",
            policy_report={},
            plan={},
            image_digest="sha256:" + "5" * 64,
            oci_layout_sha256="6" * 64,
            image_bytes=100,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all(documents)
        session.flush()
        session.add_all([*revisions, mapping, build])
        session.flush()
        session.add_all(
            [
                ClusterMappingNode(
                    id="00000000-0000-4000-8000-000000000109",
                    mapping_id=mapping_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id="00000000-0000-4000-8000-000000000110",
                    mapping_id=mapping_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=NOW,
                ),
            ]
        )
        for installation_id, digest in (
            (complete_installation_id, "7" * 64),
            (partial_installation_id, "8" * 64),
        ):
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=revision_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    recipe_build_id=build_id,
                    image_digest="sha256:" + "5" * 64,
                    plan_digest=digest,
                    plan={},
                    state="installed",
                    actor="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        session.add_all(
            [
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000111",
                    installation_id=complete_installation_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000112",
                    installation_id=complete_installation_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000113",
                    installation_id=partial_installation_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
            ]
        )
        for run_id, alias, route_state, digest in (
            (healthy_run_id, "pair-healthy", "published", "9" * 64),
            (degraded_run_id, "pair-stale", "published", "a" * 64),
            (route_failed_run_id, "pair-route-failed", "failed", "f" * 64),
        ):
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=complete_installation_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    alias=alias,
                    plan_digest=digest,
                    plan={},
                    state="running",
                    route_state=route_state,
                    actor="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        session.add_all(
            [
                RunNode(
                    id="00000000-0000-4000-8000-000000000114",
                    run_id=healthy_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=180,
                    updated_at=NOW - timedelta(seconds=1),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000115",
                    run_id=healthy_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8001,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=175,
                    updated_at=NOW - timedelta(seconds=2),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000116",
                    run_id=degraded_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8002,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=190,
                    updated_at=NOW - timedelta(seconds=3),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000122",
                    run_id=degraded_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8003,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=185,
                    updated_at=NOW - timedelta(seconds=300),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000123",
                    run_id=route_failed_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8004,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=180,
                    updated_at=NOW - timedelta(seconds=1),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000124",
                    run_id=route_failed_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8005,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=175,
                    updated_at=NOW - timedelta(seconds=2),
                ),
            ]
        )
        session.add_all(
            [
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000117",
                    node_id=NODE_A,
                    kind="disk",
                    resource_key="install",
                    amount_bytes=100,
                    owner_kind="install",
                    owner_id=complete_installation_id,
                    state="active",
                    plan_digest="b" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000118",
                    node_id=NODE_A,
                    kind="unified-memory",
                    resource_key="run",
                    amount_bytes=200,
                    owner_kind="run",
                    owner_id=healthy_run_id,
                    state="active",
                    plan_digest="c" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000119",
                    node_id=NODE_A,
                    kind="port",
                    resource_key="8000",
                    amount_bytes=0,
                    owner_kind="run",
                    owner_id=healthy_run_id,
                    state="active",
                    plan_digest="d" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000120",
                    node_id=NODE_A,
                    kind="disk",
                    resource_key="released",
                    amount_bytes=999,
                    owner_kind="install",
                    owner_id=partial_installation_id,
                    state="released",
                    plan_digest="e" * 64,
                    created_at=NOW,
                    released_at=NOW,
                ),
            ]
        )

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    from cluster_profiles.cli_render.dispatch import render_payload

    render_payload(snapshot.model_dump(mode="json"), "fleet")
    rendered = capsys.readouterr()
    assert "1 workload serving" in rendered.out
    assert rendered.out.count("Recipe: Pair Recipe") == 3
    attention = rendered.out[rendered.out.index("Needs attention") :]
    assert "pair-healthy" not in attention
    assert "pair-stale" in attention and "rank-stale" in attention
    assert "pair-route-failed" in attention and "route-not-published" in attention
    assert "incomplete" in attention
    render_payload(snapshot.model_dump(mode="json"), "fleet", wide=True)
    rendered = capsys.readouterr()
    assert NODE_A in rendered.out and NODE_B in rendered.out
    assert str(partial_installation_id) in rendered.out
    selected = snapshot.model_copy(update={"nodes": snapshot.nodes[:1]})
    render_payload(selected.model_dump(mode="json"), "fleet")
    rendered = capsys.readouterr()
    # Members outside the selection stay named rather than dropped.
    assert NODE_B in rendered.out
    alpha, beta = snapshot.nodes
    complete = next(
        value
        for value in alpha.installed
        if value.installation_id == complete_installation_id
    )
    partial = next(
        value
        for value in alpha.installed
        if value.installation_id == partial_installation_id
    )
    healthy = next(value for value in alpha.loaded if value.run_id == healthy_run_id)
    degraded = next(value for value in alpha.loaded if value.run_id == degraded_run_id)
    route_failed = next(
        value for value in alpha.loaded if value.run_id == route_failed_run_id
    )

    assert (
        complete.expected_rank_count,
        complete.present_ranks,
        complete.member_node_ids,
        complete.complete,
        complete.degraded_reason,
    ) == (2, [0, 1], [NODE_A, NODE_B], True, None)
    assert (
        partial.expected_rank_count,
        partial.present_ranks,
        partial.member_node_ids,
        partial.complete,
        partial.degraded_reason,
    ) == (2, [0], [NODE_A], False, "missing-ranks")
    assert (
        healthy.expected_rank_count,
        healthy.present_ranks,
        healthy.member_node_ids,
        healthy.healthy,
        healthy.group_state,
        healthy.degraded_reason,
    ) == (2, [0, 1], [NODE_A, NODE_B], True, "healthy", None)
    assert (
        degraded.expected_rank_count,
        degraded.present_ranks,
        degraded.member_node_ids,
        degraded.route_state,
        degraded.healthy,
        degraded.group_state,
        degraded.degraded_reason,
    ) == (
        2,
        [0, 1],
        [NODE_A, NODE_B],
        "published",
        False,
        "degraded",
        "rank-stale",
    )
    assert (
        route_failed.present_ranks,
        route_failed.member_node_ids,
        route_failed.route_state,
        route_failed.healthy,
        route_failed.degraded_reason,
    ) == ([0, 1], [NODE_A, NODE_B], "failed", False, "route-not-published")

    # A route pending its first publication, with nothing recorded against it,
    # is not degraded. A waiting publication and a health-recovery re-publish
    # carry a route_error and stay degraded.
    for route_error, expected in (
        (None, None),
        ("supervisor busy; publication waits", "route-not-published"),
        ("recipe rank health requires recovery", "route-not-published"),
    ):
        with sessions.begin() as session:
            run = session.get(RecipeRun, route_failed_run_id)
            assert run is not None
            run.route_state = "pending"
            run.route_error = route_error
        reread = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
        pending = next(v for v in reread.loaded if v.run_id == route_failed_run_id)
        assert pending.degraded_reason == expected
    assert [value.installation_id for value in beta.installed] == [
        complete_installation_id
    ]
    assert [value.run_id for value in beta.loaded] == [
        healthy_run_id,
        degraded_run_id,
        route_failed_run_id,
    ]
    assert alpha.reservations.model_dump() == {
        "disk_bytes": 100,
        "unified_memory_bytes": 200,
        "host_memory_bytes": 0,
        "gpu_memory_bytes": 0,
        "port_count": 1,
    }

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.state = NodeIdentityState.RETIRED
        node.revoked_at = NOW

    registered_visible = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    external_install = next(
        value
        for value in registered_visible.installed
        if value.installation_id == complete_installation_id
    )
    external_run = next(
        value for value in registered_visible.loaded if value.run_id == healthy_run_id
    )

    assert (
        external_install.present_ranks,
        external_install.member_node_ids,
        external_install.complete,
        external_install.degraded_reason,
    ) == ([0], [NODE_A], False, "external-member")
    assert (
        external_run.present_ranks,
        external_run.member_node_ids,
        external_run.healthy,
        external_run.group_state,
        external_run.degraded_reason,
    ) == ([0], [NODE_A], False, "degraded", "external-member")


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "damage",
    (
        "document",
        "candidate",
        "mapping",
        "mapping_rank",
        "rank",
        "state",
        "labels",
    ),
)
def test_a_damaged_active_revision_preserves_known_presence_and_recovers(
    damage: str,
) -> None:
    """A node whose catalog revision is unreadable must not read as empty.

    Skipping the group would show an operator a node with nothing installed,
    which says "nothing is deployed" rather than "the catalog is
    inconsistent". An ineligible revision is different: it is simply not this
    node's business, so it disappears without an error.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000401"
    revision_id = "00000000-0000-4000-8000-000000000402"
    model_id = "00000000-0000-4000-8000-000000000403"
    model_revision_id = "00000000-0000-4000-8000-000000000404"
    mapping_id = "00000000-0000-4000-8000-000000000405"
    build_id = "00000000-0000-4000-8000-000000000406"
    installation_id = "00000000-0000-4000-8000-000000000407"
    run_id = "00000000-0000-4000-8000-000000000408"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "broken-revision"))
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            model_id,
            model_revision_id,
            slug="broken-recipe",
            title="Broken Recipe",
        )
        mapping = ClusterMapping(
            id=mapping_id,
            recipe_revision_id=revision_id,
            topology_name="single",
            generation=1,
            node_count=1,
            state="ready",
            parameters={},
            placement_digest="2" * 64,
            endpoint_owner_node_id=NODE_A,
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        build = RecipeBuild(
            id=build_id,
            recipe_revision_id=revision_id,
            builder_node_id=NODE_A,
            source_bundle_sha256="3" * 64,
            build_input_sha256="4" * 64,
            state="succeeded",
            policy_report={},
            plan={},
            image_digest="sha256:" + "5" * 64,
            oci_layout_sha256="6" * 64,
            image_bytes=100,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all(documents)
        session.flush()
        session.add_all([*revisions, mapping, build])
        session.flush()
        session.add(
            ClusterMappingNode(
                id="00000000-0000-4000-8000-000000000409",
                mapping_id=mapping_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=revision_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=build_id,
                image_digest="sha256:" + "5" * 64,
                plan_digest="7" * 64,
                plan={},
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            InstallationNode(
                id="00000000-0000-4000-8000-000000000410",
                installation_id=installation_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=100,
                installed_bytes=100,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeRun(
                id=run_id,
                installation_id=installation_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                alias="broken-run",
                plan_digest="8" * 64,
                plan={},
                state="running",
                route_state="published",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            RunNode(
                id="00000000-0000-4000-8000-000000000411",
                run_id=run_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="running",
                port=8000,
                reserved_memory_bytes=200,
                observed_memory_bytes=180,
                updated_at=NOW,
            )
        )
    # A second known group on the same node must survive the damaged row.
    healthy_installation_id = "00000000-0000-4000-8000-000000000907"
    healthy_run_id = "00000000-0000-4000-8000-000000000908"
    with sessions.begin() as session:
        session.add(_profile(NODE_A, display_name="Spark A"))
        set_inventory_hostname(session, NODE_A, "spark-a")
        for model, source_id, replacements in (
            (
                ClusterMapping,
                mapping_id,
                {
                    "id": "00000000-0000-4000-8000-000000000905",
                    "placement_digest": "9" * 64,
                },
            ),
            (
                ClusterMappingNode,
                "00000000-0000-4000-8000-000000000409",
                {
                    "id": "00000000-0000-4000-8000-000000000909",
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                },
            ),
            (
                RecipeInstallation,
                installation_id,
                {
                    "id": healthy_installation_id,
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                },
            ),
            (
                InstallationNode,
                "00000000-0000-4000-8000-000000000410",
                {
                    "id": "00000000-0000-4000-8000-000000000910",
                    "installation_id": healthy_installation_id,
                },
            ),
            (
                RecipeRun,
                run_id,
                {
                    "id": healthy_run_id,
                    "installation_id": healthy_installation_id,
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                    "alias": "healthy-run",
                },
            ),
            (
                RunNode,
                "00000000-0000-4000-8000-000000000411",
                {
                    "id": "00000000-0000-4000-8000-000000000911",
                    "run_id": healthy_run_id,
                },
            ),
        ):
            table = cast(Table, model.__table__)
            session.execute(
                table.insert().from_select(
                    [column.name for column in table.columns],
                    select(
                        *[
                            literal(replacements[column.name])
                            if column.name in replacements
                            else column
                            for column in table.columns
                        ]
                    ).where(table.c.id == source_id),
                )
            )
    # Damage the row the way an out-of-band write would, in its own session:
    # the ORM refuses to rewrite an active revision (before_update,
    # before_delete, and a commit-time digest check), so only a restore, manual
    # surgery or drift against a newer canonical model leaves this state on disk.
    with sessions.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        if damage == "mapping":
            session.execute(
                update(ClusterMapping)
                .where(ClusterMapping.id == mapping_id)
                .values(node_count=-1)
            )
        elif damage == "mapping_rank":
            session.execute(
                update(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .values(rank=-1)
            )
        elif damage == "rank":
            session.execute(
                update(InstallationNode)
                .where(InstallationNode.installation_id == installation_id)
                .values(rank=-1)
            )
            session.execute(
                update(RunNode).where(RunNode.run_id == run_id).values(rank=-1)
            )
        elif damage == "state":
            session.execute(
                update(RecipeInstallation)
                .where(RecipeInstallation.id == installation_id)
                .values(state="invalid")
            )
            session.execute(
                update(RecipeRun).where(RecipeRun.id == run_id).values(state="invalid")
            )
        elif damage == "labels":
            session.execute(
                update(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == NODE_A)
                .values(labels=["invalid"])
            )
        elif damage == "candidate":
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision_id)
                .values(state="candidate")
            )
        else:
            session.execute(
                text(
                    "UPDATE catalog_document_revisions "
                    "SET document = :value WHERE id = :id"
                ),
                {"value": '{"schema_version": 2}', "id": revision_id},
            )
        session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    projection = FleetProjection(sessions, clock=lambda: NOW)
    if damage == "candidate":
        snapshot = projection.read()
        assert (snapshot.nodes[0].installed, snapshot.nodes[0].loaded) == ([], [])
        return
    from .test_operation_api import _client

    client, operator, *_ = _client(fleet_projection=projection)
    snapshot = projection.read()
    observed = client.get("/api/fleet", headers=operator)
    assert observed.status_code == 200
    observed_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(observed))
    )
    if damage != "labels":
        assert observed_snapshot.nodes[0].installed[0].complete is None
        assert observed_snapshot.nodes[0].loaded[0].healthy is None
    snapshot = projection.read()
    if damage == "labels":
        assert observed_snapshot.nodes[0].labels is None
        issues = observed_snapshot.nodes[0].projection_issues
        assert issues is not None and "unknown" in issues[0]
        assert all(value.complete is True for value in snapshot.nodes[0].installed)
        with sessions.begin() as session:
            session.execute(
                update(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == NODE_A)
                .values(labels={"role": "inference"})
            )
        recovered = client.get("/api/fleet", headers=operator)
        recovered_snapshot = FleetSnapshot.model_validate_json(
            canonical_message(observation_document(recovered))
        )
        assert recovered_snapshot.nodes[0].labels == {"role": "inference"}
        assert not recovered_snapshot.nodes[0].projection_issues
        return
    if damage in {"mapping", "mapping_rank", "rank", "state"}:
        assert (
            next(
                value
                for value in snapshot.nodes[0].installed
                if value.installation_id == healthy_installation_id
            ).complete
            is True
        )
        assert (
            next(
                value
                for value in snapshot.nodes[0].loaded
                if value.run_id == healthy_run_id
            ).healthy
            is True
        )
    assert snapshot.nodes[0].installed[0].installation_id == installation_id
    assert snapshot.nodes[0].loaded[0].run_id == run_id
    assert snapshot.nodes[0].installed[0].complete is None
    assert snapshot.nodes[0].loaded[0].healthy is None
    installation_issue = snapshot.nodes[0].installed[0].projection_issue
    run_issue = snapshot.nodes[0].loaded[0].projection_issue
    assert installation_issue is not None and "unknown" in installation_issue
    assert run_issue is not None and "unknown" in run_issue
    with sessions.begin() as session:
        session.execute(
            update(ClusterMapping)
            .where(ClusterMapping.id == mapping_id)
            .values(node_count=1)
        )
        session.execute(
            update(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == mapping_id)
            .values(rank=0)
        )
        session.execute(
            update(InstallationNode)
            .where(InstallationNode.installation_id == installation_id)
            .values(rank=0)
        )
        session.execute(update(RunNode).where(RunNode.run_id == run_id).values(rank=0))
        session.execute(
            update(RecipeInstallation)
            .where(RecipeInstallation.id == installation_id)
            .values(state="installed")
        )
        session.execute(
            update(RecipeRun).where(RecipeRun.id == run_id).values(state="running")
        )
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                document=revisions[0].document,
                content_digest=revisions[0].content_digest,
            )
        )
    recovered = client.get("/api/fleet", headers=operator)
    assert recovered.status_code == 200
    recovered_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(recovered))
    )
    assert recovered_snapshot.nodes[0].loaded[0].healthy is True
    restored = projection.read()
    assert restored.nodes[0].installed[0].complete is True
    assert restored.nodes[0].loaded[0].healthy is True


def test_non_rfc_non_nil_boot_id_flows_through_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active"))
        sample = _telemetry(
            NODE_A,
            "00000000-0000-4000-8000-000000000299",
            NOW - timedelta(seconds=1),
            sequence=1,
            cpu=1.0,
            boot_id=NON_RFC_BOOT_ID,
        )
        session.add(sample)
        session.flush()
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=sample.id))

    projection = FleetProjection(sessions, clock=lambda: NOW)
    snapshot = projection.read()

    node_telemetry = snapshot.nodes[0].telemetry
    assert node_telemetry is not None
    assert node_telemetry.sample.boot_id == NON_RFC_BOOT_ID
