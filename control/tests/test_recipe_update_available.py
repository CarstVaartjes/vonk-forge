"""A run on an older recipe revision is flagged, never restarted; a current one is not."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileNode,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_projection import FleetProjection
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.recipe_update_notice import (
    newest_active_revisions,
    recipe_update_notice,
)
from vonk_forge_contracts import document_sha256

from tests.test_fleet_projection import (
    NODE_A,
    NOW,
    _canonical_catalog_documents,
    _certificate,
)

RECIPE_ID = "00000000-0000-4000-8000-000000000201"
REVISION_1 = "00000000-0000-4000-8000-000000000202"
REVISION_2 = "00000000-0000-4000-8000-000000000203"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _seed(
    sessions,
    *,
    running: str,
    newest_version: str = "1.1.0",
    second_state: str = "active",
    first_release: bool = True,
) -> None:
    """One Spark running the given revision of a recipe that has two revisions."""
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "update-a"))
        documents, revisions = _canonical_catalog_documents(
            RECIPE_ID,
            REVISION_1,
            _uuid(301),
            _uuid(302),
            slug="update-recipe",
            title="Update Recipe",
        )
        first = revisions[0]
        if not first_release:
            without = deepcopy(first.document)
            without.pop("release", None)
            first.document = without
            first.content_digest = document_sha256(without)
        second_document = deepcopy(first.document)
        assert first_release or "release" not in second_document
        second_document["release"] = {
            "version": newest_version,
            "released_at": "2026-09-28",
        }
        second = CatalogDocumentRevision(
            id=REVISION_2,
            document_id=RECIPE_ID,
            kind="recipe",
            publisher=first.publisher,
            slug=first.slug,
            revision_number=2,
            schema_version=2,
            state=second_state,
            document=second_document,
            content_digest=document_sha256(second_document),
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add_all(documents)
        session.flush()
        session.add_all([*revisions, second])
        session.flush()
        mapping = ClusterMapping(
            id=_uuid(303),
            recipe_revision_id=running,
            topology_name="solo",
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
            id=_uuid(304),
            recipe_revision_id=running,
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
        session.add_all([mapping, build])
        session.flush()
        session.add(
            ClusterMappingNode(
                id=_uuid(305),
                mapping_id=mapping.id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(306),
                recipe_revision_id=running,
                mapping_id=mapping.id,
                mapping_generation=1,
                recipe_build_id=build.id,
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
            RecipeRun(
                id=_uuid(307),
                installation_id=_uuid(306),
                mapping_id=mapping.id,
                mapping_generation=1,
                alias="update-run",
                plan_digest="9" * 64,
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
                id=_uuid(308),
                run_id=_uuid(307),
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


def _get[T](session, model: type[T], identifier: str) -> T:
    row = session.get(model, identifier)
    assert row is not None
    return row


def _sessions():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_a_run_on_an_older_revision_is_flagged_without_being_restarted() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1)

    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]

    (run,) = node.loaded
    assert run.run_state == "running"
    assert run.healthy
    notice = run.recipe_update
    assert notice is not None
    assert (notice.running_revision_id, notice.newest_revision_id) == (
        REVISION_1,
        REVISION_2,
    )
    assert notice.newest_version == "1.1.0"
    assert notice.newest_released_at == "2026-09-28"
    assert notice.severity == "info"
    assert "Update available: running Update Recipe" in notice.detail
    assert "newest 1.1.0 (2026-09-28). Reload to apply." in notice.detail
    (warning,) = [w for w in node.warnings if w.code == "recipe.update_available"]
    assert warning.severity == "info"
    assert warning.detail == notice.detail


def test_a_run_on_the_newest_revision_is_not_flagged() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_2)

    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]

    assert node.loaded[0].recipe_update is None
    assert all(w.code != "recipe.update_available" for w in node.warnings)


def test_newest_is_the_highest_active_revision_and_ignores_candidates() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1, second_state="candidate")
    with sessions() as session:
        newest = newest_active_revisions(session, {RECIPE_ID})
        running = _get(session, CatalogDocumentRevision, REVISION_1)
        assert newest[RECIPE_ID].id == REVISION_1
        assert recipe_update_notice("Update Recipe", running, newest[RECIPE_ID]) is None


def test_notice_without_a_release_version_names_the_revision_number() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1, first_release=False)
    with sessions() as session:
        newest = newest_active_revisions(session, {RECIPE_ID})[RECIPE_ID]
        notice = recipe_update_notice(
            "Update Recipe", _get(session, CatalogDocumentRevision, REVISION_1), newest
        )
    assert notice is not None
    assert "running Update Recipe revision 1" in notice.detail


def test_profile_load_resolves_the_newest_active_revision() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1)
    with sessions() as session:
        identity = FleetProfileService._recipe_identity(
            session, "vonk-forge/update-recipe"
        )
    assert identity == (RECIPE_ID, REVISION_2)


def test_profile_load_ignores_a_candidate_and_keeps_the_newest_active() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1, second_state="candidate")
    with sessions() as session:
        identity = FleetProfileService._recipe_identity(
            session, "vonk-forge/update-recipe"
        )
        assert not isinstance(identity, Residue)
        _, revision_id = identity
    assert revision_id == REVISION_1


def test_profile_assignment_says_when_it_runs_an_older_revision() -> None:
    sessions = _sessions()
    _seed(sessions, running=REVISION_1)

    def loaded(revision_id: str) -> FleetProfileAssignment:
        return FleetProfileAssignment(
            id=_uuid(399),
            recipe_revision_id=revision_id,
            topology_name="solo",
            desired_state="running",
            alias="update-run",
            nodes=[
                FleetProfileNode(
                    node_id=NODE_A, rank=0, role="entrypoint", endpoint_owner=True
                )
            ],
            recipe_id=RECIPE_ID,
            recipe_title="Update Recipe",
        )

    with sessions() as session:
        recipe = _get(session, CatalogDocument, RECIPE_ID)
        newest = _get(session, CatalogDocumentRevision, REVISION_2)
        older = FleetProfileService._loaded_recipe_update(
            session, (loaded(REVISION_1),), recipe, newest, [NODE_A]
        )
        current = FleetProfileService._loaded_recipe_update(
            session, (loaded(REVISION_2),), recipe, newest, [NODE_A]
        )
        not_loaded = FleetProfileService._loaded_recipe_update(
            session, None, recipe, newest, [NODE_A]
        )
    assert older is not None and older.newest_revision_id == REVISION_2
    assert current is None and not_loaded is None


def test_fleet_output_shows_the_update_as_information_not_attention(capsys) -> None:
    from cluster_profiles.cli_render import render_payload

    sessions = _sessions()
    _seed(sessions, running=REVISION_1)
    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()

    render_payload(snapshot.model_dump(mode="json"), "fleet")
    out = capsys.readouterr().out

    assert "Updates available" in out
    before_updates = out.split("Updates available")[0]
    assert "Update available" not in before_updates
    assert out.count("Update available: running Update Recipe") == 1
    assert "Reload to apply." in out
