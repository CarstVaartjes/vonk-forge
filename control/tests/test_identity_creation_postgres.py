"""Unique-index arbitration and replay through real producer/store boundaries."""

from __future__ import annotations

import io
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, local
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    AgentOperation,
    DistributionAssignmentState,
    DistributionCode,
    LifecycleState,
)
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.admission_locking import AdmissionLockKey, node_admission_key
from vonk_control.auth import CursorCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.distribution import (
    DistributionIntegrityError,
    DistributionService,
    MemoryObjectSource,
)
from vonk_control.identity_insert import insert_identity
from vonk_control.models import (
    AgentNode,
    ArtifactDistributionAssignment,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    RecipeRun,
    RecipeSourceBundle,
)
from vonk_control.recipe_operations.interfaces import new_recipe_job
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    generate_source_bundle,
)
from vonk_forge_contracts import document_sha256

from .test_artifact_jobs import _configure_artifact_recipe
from .test_catalog_entities import NOW, _model, _recipe
from .test_distribution import _assignment
from .test_recipe_operations import installed_recipe, setup_services


@contextmanager
def simultaneous_insert(engine, table: str):
    """Both producers reach their first insert before either can commit."""
    barrier = Barrier(2)
    producer = local()

    def before(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(f"INSERT INTO {table} ") and not getattr(
            producer, "started", False
        ):
            producer.started = True
            barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before)


def test_two_bundle_uploads_share_one_verified_content_row(postgres_engine):
    # Break caught: locking a missing archive row does not serialize first puts;
    # the losing producer used to raise a unique violation at metadata flush.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})

    def upload(_index):
        store = DatabaseSourceBundleStore(sessions)
        store.put(bundle.sha256, io.BytesIO(bundle.archive))
        return store.get(bundle.sha256).manifest

    with (
        simultaneous_insert(postgres_engine, "recipe_source_bundles"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        assert list(pool.map(upload, range(2))) == [bundle.manifest] * 2
    with sessions() as session:
        assert len(session.scalars(select(RecipeSourceBundle)).all()) == 1


def test_two_catalog_imports_replay_the_exact_revision(postgres_engine):
    # Break caught: concurrent first imports both see no root, then one fails
    # the natural identity constraint instead of replaying the accepted content.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    document = _recipe(_model())

    def import_recipe(_index):
        catalog = CatalogService(
            sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
        )
        return catalog.import_recipe_library(
            "test",
            library_commit="a" * 40,
            source_path="recipe.json",
            document=document,
            expected_content_sha256=document_sha256(document),
            dependency_documents=[_model()],
        ).id

    with (
        simultaneous_insert(postgres_engine, "catalog_documents"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        identities = list(pool.map(import_recipe, range(2)))
    assert identities[0] == identities[1]
    with sessions() as session:
        assert len(session.scalars(select(CatalogDocument)).all()) == 2
        assert len(session.scalars(select(CatalogDocumentRevision)).all()) == 2
        assert {
            head.active_revision_id
            for head in session.scalars(select(CatalogDocumentHead))
        } >= {identities[0]}


def test_concurrent_activation_rolls_back_losing_run_and_replays(
    tmp_path, postgres_engine
):
    # Break caught: a request-key advisory refusal escapes a duplicate instead
    # of returning the winner. Also detects orphan runs/extra intent writes.
    sessions, operations, _queue, mapping, build, nodes = setup_services(
        tmp_path,
        engine=postgres_engine,
        recipe_transform=_configure_artifact_recipe,
    )
    installation = installed_recipe(
        operations, mapping, build, nodes, request_id=str(uuid4())
    )
    plan = operations.preview_run(installation.owner_id, "image-job")
    request_id = str(uuid4())
    start = Barrier(2)

    def activate(_index):
        start.wait(timeout=10)
        return operations.activate_job_run(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate, range(2)))
    assert results[0] == results[1]
    with sessions() as session:
        assert (
            len(session.scalars(select(Job).where(Job.request_id == request_id)).all())
            == 1
        )
        assert [row.id for row in session.scalars(select(RecipeRun))] == [
            results[0].owner_id
        ]


def test_assignment_duplicate_does_not_wait_on_existing_row_lock(postgres_engine):
    # Break caught: replay selected the identity FOR UPDATE, so a delivery
    # writer held a duplicate registration hostage until the SQL deadline.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    node = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node, state=NodeIdentityState.ACTIVE))
    assignment = _assignment(node, "1" * 64, "2" * 64, "3" * 64)
    source = MemoryObjectSource({})
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    service = DistributionService(source, sessions=sessions)
    service.register(assignment)
    holder = sessions()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert (
            holder.get(
                ArtifactDistributionAssignment,
                assignment.assignment_id,
                with_for_update=True,
            )
            is not None
        )
        pool.submit(service.register, assignment).result(timeout=10)
    finally:
        holder.rollback()
        holder.close()
        pool.shutdown(wait=True)
    changed = assignment.model_copy(
        update={"oci_image_config_digest": "sha256:" + "8" * 64}
    )
    with pytest.raises(DistributionIntegrityError) as refused:
        service.register(changed)
    assert refused.value.code == DistributionCode.ASSIGNMENT_CONFLICT
    with sessions() as session:
        row = session.get(ArtifactDistributionAssignment, assignment.assignment_id)
        assert row is not None and row.state == DistributionAssignmentState.ACTIVE
        assert row.generation == assignment.generation


def test_concurrent_first_assignment_replays_across_both_unique_keys(postgres_engine):
    # Break caught: the same assignment collides on its primary key AND its
    # plan/node key. Index-specific ON CONFLICT can deadlock on the other index.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    node = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node, state=NodeIdentityState.ACTIVE))
    assignment = _assignment(node, "1" * 64, "2" * 64, "3" * 64)
    source = MemoryObjectSource({})
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )

    def register(_index):
        service = DistributionService(source, sessions=sessions)
        service.register(assignment)
        return service.authorize(
            node_id=node, plan_digest=assignment.plan_digest
        ).assignment_id

    with (
        simultaneous_insert(postgres_engine, "artifact_distribution_assignments"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        assert list(pool.map(register, range(2))) == [assignment.assignment_id] * 2
    with sessions() as session:
        assert len(session.scalars(select(ArtifactDistributionAssignment)).all()) == 1


def test_identity_insert_keeps_canonical_json_write_validation(postgres_engine):
    # Break caught: Core INSERT bypasses ORM before_insert, silently dropping
    # the canonical producer contract that ordinary session.add enforces.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    candidate = new_recipe_job(
        id=str(uuid4()),
        request_id=str(uuid4()),
        kind=AgentOperation.RECIPE_STOP.value,
        state=LifecycleState.RUNNING.value,
        actor="admin",
        authority_revision="a" * 64,
        targets=[],
        payload_digest="b" * 64,
        payload={"malformed": True},
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        with pytest.raises(ValueError, match="jobs.payload"):
            insert_identity(session, candidate, key=("request_id",))
        assert session.scalar(select(Job)) is None


def test_admission_advisory_identity_is_node_only():
    assert node_admission_key("spark").database_key == "vonk-admission:node:spark"
    constructor = cast(Callable[..., AdmissionLockKey], AdmissionLockKey)
    with pytest.raises(TypeError):
        constructor(namespace="request", identity="same-request")


def test_identity_replay_refreshes_a_previously_observed_winner(postgres_engine):
    # Break caught: SQLAlchemy returns its cached object after the conflict,
    # hiding the committed winner's current metadata from replay validation.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    )
    catalog.import_catalog_models("test", [_model()])
    with sessions() as observer:
        observed = observer.scalars(select(CatalogDocument)).one()
        observer.commit()
        with sessions.begin() as writer:
            writer.execute(
                update(CatalogDocument)
                .where(CatalogDocument.id == observed.id)
                .values(title="Latest publisher title")
            )
        candidate = CatalogDocument(
            kind=observed.kind,
            publisher=observed.publisher,
            slug=observed.slug,
            title=observed.title,
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        replayed, created = insert_identity(
            observer, candidate, key=("kind", "publisher", "slug")
        )
        assert not created and replayed.id == observed.id
        assert replayed.title == "Latest publisher title"
