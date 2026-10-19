"""Fleet removal reuses its owners and proves exact effects after restart."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation,
    InstallationState,
    LifecycleState,
    ModelCacheCode,
)
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.fleet_model_removal_contract import FleetRemovalDisposition
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
)
from vonk_control.operation_contract import AvailabilityOperationFailure
from vonk_control.spark_store_retention import store_retention_request
from vonk_control.strict_json import read_stored_model

from .test_catalog_revision_collection import NODE, Catalog
from .test_model_cache import _artifact, _download
from .test_model_removal_reference_lifecycle import _one_model, _register_model
from .test_model_removal_worker_postgres import _prepare_removal_target
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)


def submit_model_download(cache: ModelCacheService, tmp_path: Path) -> None:
    downloaded = _download(
        cache,
        [_artifact(tmp_path, b"fresh request")],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
    )
    assert downloaded.state == LifecycleState.SUCCEEDED, downloaded.last_error


def inventory(
    sessions, node_id: str, now: datetime, objects: list[ModelStoreObject] | None
) -> None:
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id=node_id,
            observed_at=now,
            disk_total_bytes=1000,
            disk_free_bytes=900,
            host_memory_total_bytes=1000,
            host_memory_free_bytes=900,
            gpu_memory_total_bytes=1000,
            gpu_memory_free_bytes=900,
            gpu_count=1,
            memory_pool="shared",
            artifact_store_read_only=False,
            capabilities=(),
            model_objects=objects,
        )
    )


def test_finished_recipe_model_eviction_is_exact_durable_and_waits_for_evidence(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches NAS-only removal and success inferred from an eviction request.

    The managed NAS store is real; the Spark verdict is the production wire
    contract, returned through the real PostgreSQL inventory repository.
    """
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    second = "spk_" + "2" * 32
    with world.sessions.begin() as session:
        session.add(AgentNode(node_id=second, state=NodeIdentityState.ACTIVE))
    now = [datetime.now(UTC)]
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    try:
        selector, _model_digest, path = _prepare_removal_target(
            cache,
            world.sessions,
            tmp_path,
            slug="finished-model",
            data=b"finished weights",
        )
        obj = ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)
        inventory(world.sessions, NODE, now[0], [obj])
        inventory(world.sessions, second, now[0], [obj])
        review = cache.review_fleet_model_removal(selector)
        assert review.fleet is not None and review.fleet.nas_remove
        assert review.fleet.targets[0].reclaimable_bytes == len(b"finished weights")
        key = str(uuid4())
        accepted = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=key,
            review_digest=review.review_digest,
        )
        assert accepted.state == LifecycleState.QUEUED
        with world.sessions() as session:
            request = store_retention_request(session, NODE, now[0])
        assert request is not None and request.remove_model_objects == [obj.sha256]
        cache.advance_removals()
        assert cache.get_operation(accepted.id).state != LifecycleState.SUCCEEDED
        assert path.exists()
        cache.close()
        cache = ModelCacheService(
            world.sessions,
            tmp_path / "nas",
            reserve_bytes=0,
            fixture_sources=True,
            clock=lambda: now[0],
        )
        replay = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=key,
            review_digest=review.review_digest,
        )
        assert replay.id == accepted.id
        now[0] += timedelta(seconds=60)
        inventory(world.sessions, NODE, now[0], [])
        cache.advance_removals()
        assert cache.get_operation(accepted.id).state != LifecycleState.SUCCEEDED
        now[0] += timedelta(seconds=60)
        inventory(world.sessions, second, now[0], [])
        for _ in range(6):
            cache.advance_removals()
        result = cache.get_operation(accepted.id)
        assert result.state == LifecycleState.SUCCEEDED, result.last_error
        assert not path.exists()
        assert result.fleet is not None and result.fleet.confirmed_nodes == [
            NODE,
            second,
        ]
        assert result.result is not None
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()


def test_shared_content_is_retained_on_nas_and_spark(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches model-slug deletion of bytes a different model still names,
    and acceptance of a review after a pin was added.
    """
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    now = datetime.now(UTC)
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now,
    )
    try:
        selector, _, path = _prepare_removal_target(
            cache, world.sessions, tmp_path, slug="shared-model", data=b"shared weights"
        )
        _register_model(
            world.sessions, _one_model(tmp_path, "other-model", b"shared weights")
        )
        inventory(
            world.sessions,
            NODE,
            now,
            [ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)],
        )
        review = cache.review_fleet_model_removal(selector)
        assert review.fleet is not None
        assert (
            review.fleet.targets[0].objects[0].disposition
            == FleetRemovalDisposition.RETAIN
        )
        accepted = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        for _ in range(5):
            cache.advance_removals()
        assert cache.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
        assert path.read_bytes() == b"shared weights"
        with world.sessions() as session:
            request = store_retention_request(session, NODE, now)
        assert request is not None and not request.remove_model_objects
    finally:
        cache.close()


@pytest.mark.parametrize("installation_count", [1, 2])
def test_unused_installation_uninstalls_through_existing_job(
    postgres_engine, tmp_path: Path, installation_count: int
) -> None:
    """Catches a parallel deleter and siblings competing with their own parent
    for the same model. Every uninstall uses the real job and result path.
    """
    from vonk_control.cluster_mappings import ClusterMappingService

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=installation_count, mapping_node_count=1
    )
    first = installed_recipe(
        lifecycle, mapping_id, build_id, nodes[:1], request_id=str(uuid4())
    )
    with sessions() as session:
        installation = session.get(RecipeInstallation, first.owner_id)
        assert (
            installation is not None and installation.model_content_sha256 is not None
        )
        selector = installation.model_content_sha256
        revision_id = installation.recipe_revision_id
    if installation_count == 2:
        mappings = ClusterMappingService(sessions)
        mapped = mappings.preview(revision_id, nodes[1:], {}, "admin")
        second_mapping = mappings.materialize(mapped, actor="admin", now=NOW)
        installed_recipe(
            lifecycle, second_mapping, build_id, nodes[1:], request_id=str(uuid4())
        )
    now = [NOW + timedelta(seconds=1)]
    cache = ModelCacheService(
        sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    cache._fleet_lifecycle = lifecycle
    try:
        for node in nodes:
            inventory(sessions, node, now[0], [])
        review = cache.review_fleet_model_removal(selector)
        assert review.fleet is not None
        assert all(
            effect.disposition == FleetRemovalDisposition.REMOVE
            for target in review.fleet.targets
            for effect in target.installations
        )
        accepted = cache.remove_fleet_model_selector(
            selector,
            actor="admin",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        cache.advance_removals()
        completed: set[str] = set()
        for _ in range(installation_count):
            with sessions() as session:
                child = session.scalar(
                    select(Job).where(
                        Job.kind == AgentOperation.RECIPE_UNINSTALL,
                        Job.id.not_in(completed),
                    )
                )
                assert child is not None, cache.get_operation(accepted.id).last_error
                child_id = child.id
            for node in lifecycle.get(child_id).nodes:
                lifecycle.record_node_result(
                    child_id, node, succeeded=True, evidence={}
                )
            completed.add(child_id)
            now[0] += timedelta(seconds=60)
            for node in nodes:
                inventory(sessions, node, now[0], [])
            for _ in range(4):
                cache.advance_removals()
        result = cache.get_operation(accepted.id)
        assert result.state == LifecycleState.SUCCEEDED, result.last_error
        with sessions() as session:
            assert all(
                row.state == InstallationState.UNINSTALLED
                for row in session.scalars(select(RecipeInstallation))
            )
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()


@pytest.mark.parametrize("saved_profile", [True, False])
def test_profile_or_running_workload_retains_model_and_installation(
    postgres_engine, tmp_path: Path, saved_profile: bool
) -> None:
    """Catches request-led cleanup bypassing saved-profile or running owners."""
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    with sessions() as session:
        installation = session.scalar(select(RecipeInstallation))
        assert (
            installation is not None and installation.model_content_sha256 is not None
        )
        installation_id, digest = installation.id, installation.model_content_sha256
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        recipe_selector = f"{revision.publisher}/{revision.slug}"
    if saved_profile:
        from .test_unused_storage_collection import _profile

        world = object.__new__(Catalog)
        world.sessions = sessions
        _profile(world, recipe_selector, list(nodes))
    else:
        started_recipe(
            sessions, lifecycle, installation_id, nodes, request_id=str(uuid4())
        )
    now = NOW + timedelta(seconds=1)
    cache = ModelCacheService(
        sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now,
    )
    cache._fleet_lifecycle = lifecycle
    try:
        inventory(sessions, nodes[0], now, [])
        review = cache.review_fleet_model_removal(digest)
        assert review.fleet is not None and not review.fleet.nas_remove
        assert (
            review.fleet.targets[0].installations[0].disposition
            == FleetRemovalDisposition.RETAIN
        )
        accepted = cache.remove_fleet_model_selector(
            digest,
            actor="admin",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        cache.advance_removals()
        assert cache.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
        with sessions() as session:
            kept = session.get(RecipeInstallation, installation_id)
            assert kept is not None and kept.state == InstallationState.INSTALLED
            assert (
                session.scalar(
                    select(Job).where(Job.kind == AgentOperation.RECIPE_UNINSTALL)
                )
                is None
            )
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()


def test_profile_added_after_review_replans_without_fencing_or_deleting(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches acceptance of an obsolete review after a profile acquires a pin."""
    from vonk_control.fleet_profile_contract import (
        FleetProfileAssignmentInput,
        FleetProfileInput,
    )
    from vonk_control.fleet_profiles import FleetProfileService
    from vonk_control.model_cache.errors import ModelCacheConflictInvalid

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    with sessions() as session:
        installation = session.scalar(select(RecipeInstallation))
        assert (
            installation is not None and installation.model_content_sha256 is not None
        )
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        digest, recipe = (
            installation.model_content_sha256,
            f"{revision.publisher}/{revision.slug}",
        )
    now = NOW + timedelta(seconds=1)
    cache = ModelCacheService(
        sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now,
    )
    cache._fleet_lifecycle = lifecycle
    try:
        inventory(sessions, nodes[0], now, [])
        old = cache.review_fleet_model_removal(digest)
        profiles = FleetProfileService(sessions, clock=lambda: now)
        profiles.create(
            FleetProfileInput(
                name="Owner profile",
                assignments=[
                    FleetProfileAssignmentInput(
                        recipe_selector=recipe, spark_ids=list(nodes)
                    )
                ],
            ),
            actor="admin",
        )
        with pytest.raises(ModelCacheConflictInvalid):
            cache.remove_fleet_model_selector(
                digest,
                actor="admin",
                request_key=str(uuid4()),
                review_digest=old.review_digest,
            )
        current = cache.review_fleet_model_removal(digest)
        assert current.fleet is not None and not current.fleet.nas_remove
        receipt = cache.remove_fleet_model_selector(
            digest,
            actor="admin",
            request_key=str(uuid4()),
            review_digest=current.review_digest,
        )
        cache.advance_removals()
        assert cache.get_operation(receipt.id).state == LifecycleState.SUCCEEDED
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["deadline", "payload", "owner", "gate"])
def test_lost_owner_authority_expires_before_fresh_reference(
    postgres_engine, tmp_path: Path, damage: str
) -> None:
    """Catches an everlasting wait or releasing deletion gates while an agent
    still has a fresh eviction authority from the failed owner.
    """
    from vonk_control.fleet_model_removal_contract import STORE_RETENTION_MAX_AGE
    from vonk_control.models import ArtifactLifecycleGate, ModelCacheOperation

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    now = [datetime.now(UTC)]
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    try:
        selector, _, path = _prepare_removal_target(
            cache,
            world.sessions,
            tmp_path,
            slug="bounded-wait",
            data=b"waiting weights",
        )
        inventory(
            world.sessions,
            NODE,
            now[0],
            [ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)],
        )
        review = cache.review_fleet_model_removal(selector)
        receipt = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        assert receipt.fleet is not None
        with world.sessions() as session:
            grant = store_retention_request(session, NODE, now[0])
            assert grant is not None and grant.remove_model_objects == [path.name]
        if damage == "deadline":
            now[0] = receipt.fleet.deadline_at + timedelta(seconds=1)
        else:
            with world.sessions.begin() as session:
                row = session.get(ModelCacheOperation, receipt.id)
                assert row is not None
                if damage == "payload":
                    row.payload = {}
                elif damage == "owner":
                    session.delete(row)
                else:
                    gate = session.get(
                        ArtifactLifecycleGate, ("model-object", path.name)
                    )
                    assert gate is not None
                    gate.removal_fence = str(uuid4())
        cache.advance_removals()
        if damage != "owner":
            ended = cache.get_operation(receipt.id)
            assert ended.state == LifecycleState.FAILED
            assert ended.failure is not None
            failure = read_stored_model(
                AvailabilityOperationFailure, ended.failure, from_json=True
            )
            assert failure.code == (
                ModelCacheCode.DOCUMENT_UNREADABLE
                if damage == "payload"
                else ModelCacheCode.REMOVAL_WAIT
            )
        assert path.exists()
        with world.sessions() as session:
            grant = store_retention_request(session, NODE, now[0])
            assert grant is not None and grant.remove_model_objects == []
        cache.reconcile_removal_gates()
        with world.sessions() as session:
            gate = session.get(ArtifactLifecycleGate, ("model-object", path.name))
            assert gate is not None
            assert (gate.removal_owner_id is None) == (damage == "deadline")
        # A lost owner retains its committed maximum authority bound. A
        # terminal owner can no longer renew a grant, so only its last TTL is
        # needed. Neither missing JSON nor a missing row becomes a forever wait.
        now[0] = (
            receipt.fleet.deadline_at + timedelta(seconds=1)
            if damage == "owner"
            else now[0] + STORE_RETENTION_MAX_AGE + timedelta(seconds=1)
        )
        # The bounded inventory cursor may be past this held gate. Let it wrap
        # through the normal next worker pass instead of targeting it manually.
        for _ in range(2):
            cache.reconcile_removal_gates()
        with world.sessions() as session:
            gate = session.get(ArtifactLifecycleGate, ("model-object", path.name))
            row = session.get(ModelCacheOperation, receipt.id)
            assert gate is not None and gate.removal_owner_id is None, (
                now[0],
                None if row is None else (row.state, row.updated_at, row.completed_at),
                None if gate is None else gate.removal_authority_expires_at,
            )
        # Reacquire the exact content reference, rather than proving only that
        # unrelated work can proceed after the failed request.
        from vonk_control.artifact_lifecycle import (
            ArtifactIdentity,
            require_reference_open,
        )

        with world.sessions.begin() as session:
            require_reference_open(
                session, [ArtifactIdentity("model-object", path.name)], now=now[0]
            )
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()


def test_changed_persisted_scope_never_authorizes_remote_eviction(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches inventory trusting a structurally valid plan whose effects no
    longer match the operator's accepted digest.
    """
    from vonk_control.model_cache_contract import ModelCacheRemovalPayload
    from vonk_control.models import ModelCacheOperation

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    now = datetime.now(UTC)
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now,
    )
    try:
        selector, _, path = _prepare_removal_target(
            cache,
            world.sessions,
            tmp_path,
            slug="scope-integrity",
            data=b"accepted weights",
        )
        inventory(
            world.sessions,
            NODE,
            now,
            [ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)],
        )
        review = cache.review_fleet_model_removal(selector)
        receipt = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        with world.sessions.begin() as session:
            row = session.get(ModelCacheOperation, receipt.id)
            assert row is not None
            from vonk_control.strict_json import read_stored_model

            plan = read_stored_model(
                ModelCacheRemovalPayload, row.payload, from_json=True
            )
            assert plan.fleet is not None
            plan.fleet.effects.targets[0].objects[0].object = ModelStoreObject(
                sha256="f" * 64, bytes=1
            )
            from vonk_control.model_cache.persistence import _store_operation_payload

            _store_operation_payload(row, "remove", plan)
        with world.sessions() as session:
            request = store_retention_request(session, NODE, now)
        assert request is not None and request.remove_model_objects == []
        assert path.read_bytes() == b"accepted weights"
        submit_model_download(cache, tmp_path)
    finally:
        cache.close()
