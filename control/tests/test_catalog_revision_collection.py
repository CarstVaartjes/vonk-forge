"""Superseded catalog revisions nothing uses any more are removed.

A recipe refresh moves the head to a new revision and leaves the old one behind.
The collector removes that old revision once it is proven unused, keeps it while
a workload, operation or image authorization still names it, and never lets one
refusal stop the rest.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.catalog_revision_collection import (
    GRACE,
    INTERVAL,
    CatalogRevisionCollector,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    RunNode,
    RuntimeImageAuthorization,
    SourceBundleArchive,
)
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    generate_source_bundle,
)

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
OLD = NOW - GRACE - timedelta(hours=2)
NODE = "spk_" + "1" * 32


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _row[T](session: Session, model: type[T], row_id: str) -> T:
    row = session.get(model, row_id)
    assert row is not None
    return row


class Catalog:
    """Rows written straight to the tables, as an older Controller left them."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.now = NOW
        self.documents: dict[str, str] = {}
        with self.sessions.begin() as session:
            session.add(
                AgentNode(node_id=NODE, state="active", workload_intent_ordinal=1)
            )

    def collector(self) -> CatalogRevisionCollector:
        return CatalogRevisionCollector(self.sessions, clock=lambda: self.now)

    def revision(
        self,
        slug: str,
        number: int,
        *,
        kind: str = "recipe",
        state: str = "active",
        created: datetime = OLD,
        head: str | None = None,
        document: object | None = None,
        projected: object | None = None,
    ) -> str:
        """Add revision ``number`` of a document; ``head`` is "active" or "candidate"."""

        revision_id = str(uuid.uuid4())
        with self.sessions.begin() as session:
            document_id = self.documents.get(f"{kind}/{slug}")
            if document_id is None:
                document_id = str(uuid.uuid4())
                self.documents[f"{kind}/{slug}"] = document_id
                session.add(
                    CatalogDocument(
                        id=document_id,
                        kind=kind,
                        publisher="vonk-forge",
                        slug=slug,
                        title=slug,
                        created_by="test",
                        created_at=created,
                        updated_at=created,
                    )
                )
                session.add(
                    CatalogDocumentHead(
                        kind=kind, publisher="vonk-forge", slug=slug, generation=0
                    )
                )
                session.flush()
            session.execute(
                insert(CatalogDocumentRevision).values(
                    id=revision_id,
                    document_id=document_id,
                    kind=kind,
                    publisher="vonk-forge",
                    slug=slug,
                    revision_number=number,
                    schema_version=2,
                    state=state,
                    # Deliberately not the document the digest was taken of:
                    # an old-contract row cannot be read or re-verified.
                    document={"legacy": slug, "number": number}
                    if document is None
                    else document,
                    content_digest=_digest(f"{slug}/{number}"),
                    projected={} if projected is None else projected,
                    created_by="test",
                    created_at=created,
                )
            )
            if head is not None:
                column = {
                    "active": CatalogDocumentHead.active_revision_id,
                    "candidate": CatalogDocumentHead.candidate_revision_id,
                }[head]
                session.execute(
                    update(CatalogDocumentHead)
                    .where(CatalogDocumentHead.slug == slug)
                    .values({column.key: revision_id})
                )
        return revision_id

    def exists(self, revision_id: str) -> bool:
        with self.sessions() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(CatalogDocumentRevision)
                    .where(CatalogDocumentRevision.id == revision_id)
                )
                == 1
            )

    def count(self, model) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def workload(
        self,
        revision_id: str,
        *,
        installation: str = "installed",
        run: str = "running",
        touched: datetime = OLD,
    ) -> tuple[str, str]:
        """An installation of the revision with one run and its mapping."""

        with self.sessions.begin() as session:
            mapping = ClusterMapping(
                recipe_revision_id=revision_id,
                topology_name="single",
                generation=1,
                node_count=1,
                state="ready",
                parameters={},
                placement_digest=_digest(f"placement/{uuid.uuid4()}"),
                endpoint_owner_node_id=NODE,
                created_by="test",
                created_at=touched,
                updated_at=touched,
            )
            session.add(mapping)
            session.flush()
            session.add(
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=NODE,
                    rank=0,
                    role="main",
                    endpoint_owner=True,
                    created_at=touched,
                )
            )
            installed = RecipeInstallation(
                recipe_revision_id=revision_id,
                mapping_id=mapping.id,
                mapping_generation=1,
                image_digest="sha256:" + "a" * 64,
                plan_digest=_digest(f"plan/{uuid.uuid4()}"),
                plan={},
                state=installation,
                actor="test",
                created_at=touched,
                updated_at=touched,
            )
            session.add(installed)
            session.flush()
            session.add(
                InstallationNode(
                    installation_id=installed.id,
                    node_id=NODE,
                    rank=0,
                    role="main",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=touched,
                )
            )
            workload = RecipeRun(
                installation_id=installed.id,
                mapping_id=mapping.id,
                mapping_generation=1,
                alias="chat",
                plan_digest=_digest(f"run/{uuid.uuid4()}"),
                plan={},
                state=run,
                actor="test",
                created_at=touched,
                updated_at=touched,
            )
            session.add(workload)
            session.flush()
            session.add(
                RunNode(
                    run_id=workload.id,
                    node_id=NODE,
                    rank=0,
                    role="main",
                    state=run,
                    port=8000,
                    reserved_memory_bytes=1,
                    updated_at=touched,
                )
            )
            return installed.id, workload.id

    def job(self, payload: object, *, state: str, updated: datetime) -> None:
        with self.sessions.begin() as session:
            session.add(
                Job(
                    request_id=str(uuid.uuid4()),
                    kind="recipe.run-switch.v2",
                    state=state,
                    actor="test",
                    authority_revision="r",
                    targets=[],
                    payload_digest="0" * 64,
                    payload=payload,
                    current_attempt=0,
                    created_at=updated,
                    updated_at=updated,
                )
            )

    def build(self, revision_id: str, *, state: str = "succeeded") -> str:
        build_id = str(uuid.uuid4())
        with self.sessions.begin() as session:
            session.add(
                RecipeBuild(
                    id=build_id,
                    recipe_revision_id=revision_id,
                    builder_node_id=NODE,
                    source_bundle_sha256="c" * 64,
                    build_input_sha256=_digest(f"input/{build_id}"),
                    state=state,
                    policy_report={},
                    plan={},
                    created_at=OLD,
                    updated_at=OLD,
                )
            )
        return build_id

    def authorize(
        self, revision_id: str, *, build_id: str, original_digest: str
    ) -> None:
        with self.sessions.begin() as session:
            session.add(
                RuntimeImageAuthorization(
                    recipe_revision_id=revision_id,
                    original_content_digest=original_digest,
                    effective_execution_key="e" * 64,
                    image_digest="sha256:" + "a" * 64,
                    local_image_config_id="sha256:" + "b" * 64,
                    oci_archive_sha256="d" * 64,
                    image_bytes=1,
                    build_id=build_id,
                    authorized_at=OLD,
                    state="authorized",
                )
            )


@pytest.fixture
def catalog(tmp_path) -> Catalog:
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record) -> None:
        # The database's own RESTRICT foreign keys are the backstop.
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Catalog(engine)


def _refresh(catalog: Catalog, slug: str) -> tuple[str, str]:
    """A recipe at revision 1, refreshed in place to revision 2 long ago."""

    old = catalog.revision(slug, 1)
    head = catalog.revision(slug, 2, head="active")
    return old, head


def test_superseded_unreferenced_revision_is_removed_after_the_grace_period(
    catalog: Catalog,
) -> None:
    """Catches a collector that never removes, or removes at supersession."""

    old, head = _refresh(catalog, "glm")
    recent_old = catalog.revision("fresh", 1, created=NOW - timedelta(hours=1))
    catalog.revision("fresh", 2, created=NOW - timedelta(hours=1), head="active")

    result = catalog.collector().collect()

    assert not catalog.exists(old)
    assert catalog.exists(head)
    # Superseded less than a grace period ago: a reload may still read it.
    assert catalog.exists(recent_old)
    assert result.revisions == 1

    catalog.now = NOW + GRACE
    catalog.collector().collect()
    assert not catalog.exists(recent_old)


def test_revision_of_an_installed_workload_is_kept_until_the_workload_moves(
    catalog: Catalog,
) -> None:
    """Catches removal of the revision a running workload was installed from."""

    old, head = _refresh(catalog, "glm")
    installation, run = catalog.workload(old)

    catalog.collector().collect()

    assert catalog.exists(old)
    with catalog.sessions() as session:
        assert session.get(RecipeInstallation, installation) is not None
        assert session.get(RecipeRun, run) is not None

    # The workload moves: it is stopped and uninstalled. Once that has been
    # quiet for the grace period the revision and its dead rows go together.
    with catalog.sessions.begin() as session:
        _row(session, RecipeRun, run).state = "stopped"
        _row(session, RecipeInstallation, installation).state = "uninstalled"
    catalog.collector().collect()

    assert not catalog.exists(old)
    assert catalog.exists(head)
    for model in (RecipeInstallation, RecipeRun, ClusterMapping, RunNode):
        assert catalog.count(model) == 0


@pytest.mark.parametrize(
    ("installation", "run"),
    [
        ("installing", "stopped"),
        ("partial", "stopped"),
        ("failed", "stopped"),
        ("uninstalled", "stopping"),
        ("uninstalled", "lost"),
        ("uninstalled", "failed"),
    ],
)
def test_a_workload_that_may_still_have_effects_keeps_its_revision(
    catalog: Catalog, installation: str, run: str
) -> None:
    """Catches treating any non-running state as dead (failed/lost may have effects)."""

    old, _head = _refresh(catalog, "glm")
    catalog.workload(old, installation=installation, run=run)

    catalog.collector().collect()

    assert catalog.exists(old)


def test_a_head_is_never_removed(catalog: Catalog) -> None:
    """Catches pruning by age or state instead of by head pointer."""

    active = catalog.revision("doc-a", 1, head="active")
    # A document whose newest revision is a failed candidate keeps both, so its
    # revision numbers are never reused.
    kept = catalog.revision("doc-b", 1, head="active")
    failed = catalog.revision("doc-b", 2, state="failed")
    candidate = catalog.revision("doc-c", 1, state="candidate", head="candidate")

    catalog.now = NOW + timedelta(days=365)
    catalog.collector().collect()

    for revision in (active, kept, failed, candidate):
        assert catalog.exists(revision)


def test_an_unreadable_old_contract_revision_is_removed_without_being_read(
    catalog: Catalog,
) -> None:
    """Catches a collector that parses or re-hashes the document before removing."""

    old = catalog.revision("legacy", 1, document=["not", "a", "document"])
    catalog.revision(
        "legacy", 2, head="active", document={"schema_version": 1, "oops": True}
    )
    bad_projection = catalog.revision(
        "legacy-b", 1, projected={"schema_version": 0, "garbage": []}
    )
    catalog.revision("legacy-b", 2, head="active")

    catalog.collector().collect()

    assert not catalog.exists(old)
    assert not catalog.exists(bad_projection)


def test_a_payload_naming_the_revision_keeps_it_whatever_contract_wrote_it(
    catalog: Catalog,
) -> None:
    """Catches checking only foreign keys: a Job payload carries ids with no FK."""

    in_flight, _ = _refresh(catalog, "in-flight")
    finished_recently, _ = _refresh(catalog, "finished-recently")
    finished_long_ago, _ = _refresh(catalog, "finished-long-ago")
    catalog.job({"plan": {"x": [in_flight]}}, state="running", updated=OLD)
    catalog.job(
        {"recipe": {"recipe_revision_id": finished_recently}},
        state="succeeded",
        updated=NOW - timedelta(hours=1),
    )
    catalog.job({"id": finished_long_ago}, state="succeeded", updated=OLD)

    catalog.collector().collect()

    assert catalog.exists(in_flight)
    assert catalog.exists(finished_recently)
    assert not catalog.exists(finished_long_ago)


def test_the_selected_profile_application_keeps_the_revision_it_loaded(
    catalog: Catalog,
) -> None:
    """Catches ignoring the loaded profile once its application has succeeded."""

    old, _head = _refresh(catalog, "glm")
    with catalog.sessions.begin() as session:
        profile = FleetProfile(
            number=1,
            name="p",
            created_by="test",
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(profile)
        session.flush()
        application = FleetProfileApplication(
            request_key=str(uuid.uuid4()),
            profile_id=profile.id,
            profile_digest="1" * 64,
            plan_digest="2" * 64,
            state="succeeded",
            plan={"assignments": [{"recipe_revision_id": old}]},
            actor="test",
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(application)
        session.flush()
        session.add(
            FleetProfileSelection(
                singleton_id=1,
                generation=1,
                profile_id=profile.id,
                profile_revision=1,
                application_id=application.id,
                roster_digest="3" * 64,
                updated_at=OLD,
            )
        )

    catalog.collector().collect()

    assert catalog.exists(old)


def test_an_image_authorization_reusing_the_original_keeps_it(
    catalog: Catalog,
) -> None:
    """Catches removing the original while the head reuses its image receipt."""

    original, head = _refresh(catalog, "glm")
    build = catalog.build(original)
    catalog.authorize(head, build_id=build, original_digest=_digest("glm/1"))

    catalog.collector().collect()

    assert catalog.exists(original)
    assert catalog.count(RecipeBuild) == 1

    with catalog.sessions.begin() as session:
        session.execute(delete(RuntimeImageAuthorization))
    catalog.collector().collect()

    assert not catalog.exists(original)
    assert catalog.count(RecipeBuild) == 0


def test_an_authorization_naming_the_original_digest_keeps_it_without_a_shared_build(
    catalog: Catalog,
) -> None:
    """Catches relying on build rows alone to protect an editorial original."""

    original, head = _refresh(catalog, "glm")
    own_build = catalog.build(head)
    catalog.authorize(head, build_id=own_build, original_digest=_digest("glm/1"))

    catalog.collector().collect()

    assert catalog.exists(original)


def test_a_workload_that_only_just_moved_keeps_its_revision_through_the_grace_period(
    catalog: Catalog,
) -> None:
    """Catches removing a revision the moment its workload stops (reconcile window)."""

    old, _head = _refresh(catalog, "glm")
    catalog.workload(
        old,
        installation="uninstalled",
        run="stopped",
        touched=NOW - timedelta(hours=1),
    )

    catalog.collector().collect()
    assert catalog.exists(old)

    catalog.now = NOW + GRACE
    catalog.collector().collect()
    assert not catalog.exists(old)


def test_an_unfinished_build_keeps_its_revision_and_a_finished_one_goes(
    catalog: Catalog,
) -> None:
    """Catches deleting a revision under a build that is still running."""

    building, _ = _refresh(catalog, "building")
    finished, _ = _refresh(catalog, "finished")
    catalog.build(building, state="building")
    catalog.build(finished)

    catalog.collector().collect()

    assert catalog.exists(building)
    assert not catalog.exists(finished)
    assert catalog.count(RecipeBuild) == 1


def test_a_model_revision_follows_the_recipe_revisions_that_pin_it(
    catalog: Catalog,
) -> None:
    """Catches removing a model a kept recipe still binds, or never removing it."""

    pinned = catalog.revision("weights", 1, kind="model")
    catalog.revision("weights", 2, kind="model", head="active")
    recipe_old = catalog.revision("glm", 1)
    recipe_head = catalog.revision("glm", 2, head="active")
    unpinned = catalog.revision("other", 1, kind="model")
    catalog.revision("other", 2, kind="model", head="active")
    with catalog.sessions.begin() as session:
        for recipe in (recipe_old, recipe_head):
            session.add(
                CatalogRecipeModelReference(
                    recipe_revision_id=recipe,
                    selection_id="primary",
                    model_revision_id=pinned,
                    model_kind="model",
                    model_publisher="vonk-forge",
                    model_slug="weights",
                    model_content_digest=_digest("weights/1"),
                )
            )

    catalog.collector().collect()

    # The head recipe still pins the old model revision; the unpinned one goes.
    assert not catalog.exists(recipe_old)
    assert catalog.exists(pinned)
    assert not catalog.exists(unpinned)

    # The recipe moves to the newer model, and the old model revision follows.
    with catalog.sessions.begin() as session:
        session.execute(delete(CatalogRecipeModelReference))
    catalog.collector().collect()
    assert not catalog.exists(pinned)


def test_one_refused_removal_does_not_stop_the_rest(catalog: Catalog) -> None:
    """Catches a sweep that aborts on the first row the database refuses."""

    refused, _ = _refresh(catalog, "refused")
    others = [_refresh(catalog, f"other-{index}")[0] for index in range(3)]
    with catalog.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER refuse BEFORE DELETE ON catalog_document_revisions "
                f"WHEN old.id = '{refused}' "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        )

    result = catalog.collector().collect()

    assert catalog.exists(refused)
    assert not any(catalog.exists(revision) for revision in others)
    assert result.revisions == 3


def test_source_bundles_nothing_names_are_removed_after_the_grace_period(
    catalog: Catalog,
) -> None:
    """Catches removing a bundle a revision names, or one stored moments ago."""

    named, orphan, fresh = "1" * 64, "2" * 64, "3" * 64
    catalog.revision("glm", 1, head="active", projected={"source_bundle_sha256": named})
    with catalog.sessions.begin() as session:
        for sha, verified in ((named, OLD), (orphan, OLD), (fresh, NOW)):
            session.add(
                RecipeSourceBundle(
                    sha256=sha,
                    media_type="application/x-tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key=f"postgres:{sha}",
                    manifest={},
                    verified_at=verified,
                )
            )
            session.add(SourceBundleArchive(sha256=sha, archive=b"x"))

    result = catalog.collector().collect()

    with catalog.sessions() as session:
        left = set(session.scalars(select(RecipeSourceBundle.sha256)))
        archives = set(session.scalars(select(SourceBundleArchive.sha256)))
    assert left == archives == {named, fresh}
    assert result.bundles == 1


def test_storing_a_bundle_again_gives_it_a_fresh_grace_period(
    catalog: Catalog,
) -> None:
    """Catches a re-put bundle still looking old to the sweep before its revision lands."""

    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store = DatabaseSourceBundleStore(catalog.sessions)
    store.put(bundle.sha256, io.BytesIO(bundle.archive))
    with catalog.sessions.begin() as session:
        _row(session, RecipeSourceBundle, bundle.sha256).verified_at = OLD

    store.put(bundle.sha256, io.BytesIO(bundle.archive))
    result = catalog.collector().collect()

    assert result.bundles == 0
    assert catalog.count(SourceBundleArchive) == 1


def test_the_sweep_runs_once_per_interval(catalog: Catalog) -> None:
    """Catches a collector that scans the catalog on every worker pass."""

    collector = catalog.collector()
    first, _ = _refresh(catalog, "one")
    assert collector.tick() is True
    assert not catalog.exists(first)

    second, _ = _refresh(catalog, "two")
    catalog.now = NOW + INTERVAL / 2
    assert collector.tick() is False
    assert catalog.exists(second)

    catalog.now = NOW + INTERVAL
    assert collector.tick() is True
    assert not catalog.exists(second)


def test_removal_succeeds_on_postgres_with_its_real_constraints(
    postgres_engine: Engine,
) -> None:
    """Catches delete ordering or correlated deletes that only SQLite accepts."""

    Base.metadata.create_all(postgres_engine)
    catalog = Catalog(postgres_engine)
    old, head = _refresh(catalog, "glm")
    installation, run = catalog.workload(old)
    kept, _ = _refresh(catalog, "kept")
    catalog.workload(kept)
    catalog.build(old)
    with catalog.sessions.begin() as session:
        _row(session, RecipeRun, run).state = "stopped"
        _row(session, RecipeInstallation, installation).state = "uninstalled"

    result = catalog.collector().collect()

    assert not catalog.exists(old)
    assert catalog.exists(head)
    assert catalog.exists(kept)
    assert result.revisions == 1
    for model in (RecipeInstallation, RecipeRun, ClusterMapping, RecipeBuild):
        assert catalog.count(model) == (1 if model is not RecipeBuild else 0)
