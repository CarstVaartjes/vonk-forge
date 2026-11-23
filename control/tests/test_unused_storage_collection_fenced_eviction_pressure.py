"""Unused storage collection: fenced eviction pressure."""

from __future__ import annotations

import copy
import json
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from vonk_agent_protocol.agent_words import (
    ProfileEffectState,
    ProfileSwitchChildKind,
)
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.model_cache import ModelCacheCode
from vonk_agent_protocol.recipe_operations import RecipeInstallPayload
from vonk_agent_protocol.state_machines import (
    InstallationState,
    NodeIdentityState,
    RunState,
)
from vonk_control.artifact_jobs.lifecycle import (
    ArtifactIdentity,
    require_reference_open,
)
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
    FleetProfileEffectProgress,
)
from vonk_control.fleet_profile_contract.review import FleetProfilePreview
from vonk_control.jobs.documents import InstallPhaseOperation, RecipeInstallParent
from vonk_control.model_cache.artifacts import ArtifactSetManifest, ArtifactSpec
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base
from vonk_control.models.cache import (
    ArtifactLifecycleGate,
    ModelCacheOperation,
    ModelCacheSet,
)
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.models.fleet import (
    AgentNode,
    FleetProfileApplication,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import (
    ArtifactDistributionAssignment,
)
from vonk_control.recipe_operations.execution_contract import (
    parse_stored_installation_plan,
)
from vonk_control.settings import (
    FREE_SPACE_FOR_COLOCATED_SERVICES_FRACTION,
    STORAGE_LOW_FREE_FRACTION,
)
from vonk_control.storage_demands import (
    NAS_MODELS,
    STORAGE_EVICTING,
    STORAGE_INSUFFICIENT,
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection import evidence as collector_evidence
from vonk_control.unused_storage_collection.common import ACTOR, spark_eviction_capacity
from vonk_control.unused_storage_collection.evidence import _Evidence
from vonk_control.unused_storage_collection.references import _installation_kept
from vonk_control.unused_storage_collection.service import UnusedStorageCollector

from .model_cache_support import _artifact, _download
from .removal_settle import release_ended_gates
from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog
from .test_model_removal_decided_by_evidence import _installation
from .test_run_switch_build_fencing import _direct_parent
from .unused_storage_collection_support import (
    _REAL_CLOCK,
    A_MODEL,
    AN_IMAGE,
    GIB,
    TOTAL,
    FakeLifecycle,
    _application,
    _collector,
    _hard_linked_models,
    _head_recipe_naming,
    _kept,
    _model_objects,
    _models,
    _profile,
    _real_lifecycle,
    _receipt,
    _report_disk,
    _required,
    _scope,
    _set_exists,
    _settle,
    _size,
    _three_idle,
    _Volume,
    _within_grace,
)


def test_unidentified_model_set_is_evicted_through_exact_fenced_scope(
    world: Catalog, cached
) -> None:
    service, set_digest = cached
    with world.sessions.begin() as session:
        model_set = session.get(ModelCacheSet, set_digest)
        assert model_set is not None
        model_set.model_content_sha256 = None
    result = _models(world, service).collect()
    assert result.models == 1, result.kept
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_zero_verified_counter_does_not_hide_present_model_bytes(
    world: Catalog, cached
) -> None:
    service, set_digest = cached
    with world.sessions.begin() as session:
        model_set = session.get(ModelCacheSet, set_digest)
        assert model_set is not None
        model_set.verified_bytes = 0
    result = _models(world, service).collect()
    assert result.models == 1, result.kept
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_failed_removal_releases_gate_and_fresh_download_reuses_model(
    world: Catalog,
    cached,
    tmp_path: Path,
) -> None:
    service, set_digest = cached
    removal = service.accept_unused_removal(
        A_MODEL,
        actor=ACTOR,
        request_key=str(uuid.uuid4()),
        verify=lambda _session, _sets: None,
    )
    with world.sessions.begin() as session:
        owner = session.get(ModelCacheOperation, removal.id)
        assert owner is not None
        owner.state = "failed"
    # An in-flight filesystem step must finish before its gate can be released.
    with service._model_storage_lock(set_digest, model_set=True):
        service.reconcile_removal_gates()
        with world.sessions() as session:
            gate = session.get(
                ArtifactLifecycleGate,
                {"artifact_kind": "model-set", "artifact_sha256": set_digest},
            )
            assert gate is not None
            assert gate.removal_owner_id == removal.id
    # A complete bounded inventory cycle revisits the previously busy gate.
    release_ended_gates(service)
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 == set_digest


@pytest.mark.parametrize(
    "fault", ["none", "before-unlink", "after-unlink", "not-retired"]
)
def test_receipt_unlink_is_outside_sql_with_committed_fence_and_fresh_admission(
    postgres_engine: Engine, tmp_path: Path, monkeypatch, fault: str
) -> None:
    """Catches filesystem effects under SQL locks and gates leaked on failure."""

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    path = _receipt(tmp_path, AN_IMAGE)
    collector = _collector(world, image_cache_root=tmp_path)
    storage = collector._images
    assert storage is not None
    original = storage.remove_published
    attempts = []

    def unlink(archive):
        # A second transaction can take the gate NOWAIT, proving the unlink
        # caller holds no SQL locks. The committed fence still denies attachment.
        with world.sessions.begin() as session:
            gate = session.scalar(
                select(ArtifactLifecycleGate)
                .where(ArtifactLifecycleGate.artifact_sha256 == archive)
                .with_for_update(nowait=True)
            )
            assert gate is not None
            assert gate.removal_owner_id is not None
            blocked = False
            try:
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", archive),),
                    now=world.now,
                )
            except Exception:  # noqa: BLE001 - inspect actual attachment effect
                blocked = True
            assert blocked
        attempts.append(archive)
        if fault == "before-unlink" and len(attempts) == 1:
            raise OSError("storage temporarily unavailable")
        if fault == "not-retired" and len(attempts) == 1:
            return 0
        result = original(archive)
        if fault == "after-unlink" and len(attempts) == 1:
            raise OSError("unlink reply lost")
        return result

    monkeypatch.setattr(storage, "remove_published", unlink)
    collector.collect()
    with world.sessions.begin() as session:
        require_reference_open(
            session, (ArtifactIdentity("runtime-image", AN_IMAGE),), now=world.now
        )
    if path.exists():
        # Ended sweep owns no gate; a fresh sweep observes bytes and completes.
        fresh = _collector(world, image_cache_root=tmp_path)
        from vonk_control.persisted_waits import read_wait

        with world.sessions() as session:
            gate = session.get(
                ArtifactLifecycleGate,
                {
                    "artifact_kind": "runtime-image",
                    "artifact_sha256": AN_IMAGE,
                },
            )
            wait = read_wait(gate) if gate is not None else None
        if wait is not None:
            assert fresh.collect().images == 0  # restart retains the bounded retry
            world.now = wait.next_check
        assert fresh.collect().images == 1
    assert not path.exists()
    assert attempts == [AN_IMAGE]
    _receipt(tmp_path, AN_IMAGE)
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1


def test_orphaned_receipt_removal_fence_is_reconciled_by_a_fresh_sweep(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches process death after intent commit leaving permanent cache busy."""
    path = _receipt(tmp_path, AN_IMAGE)
    with world.sessions.begin() as session:
        session.add(
            ArtifactLifecycleGate(
                artifact_kind="runtime-image",
                artifact_sha256=AN_IMAGE,
                removal_owner_kind="recipe-image-job",
                removal_owner_id=str(uuid.uuid4()),
                removal_fence=str(uuid.uuid4()),
                updated_at=world.now,
            )
        )
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not path.exists()
    with world.sessions() as session:
        gate = session.scalar(select(ArtifactLifecycleGate))
        # An open gate is deleted where it is released.
        assert gate is None
    fresh = _receipt(tmp_path, AN_IMAGE)
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not fresh.exists()


@pytest.mark.parametrize("hold_source", ["application", "job"])
def test_live_two_spark_owner_does_not_pin_unrelated_installations(
    postgres_engine: Engine, tmp_path: Path, hold_source: str
) -> None:
    """Fault: a copy remains live on B while A needs eviction for another load.

    Catches node ownership or a profile scope retaining every installation.
    Exact installation/run references and a running workload must still survive.
    """

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    other = "spk_" + "2" * 32
    with world.sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=other, state=NodeIdentityState.ACTIVE, workload_intent_ordinal=1
            )
        )
    revision = world.revision("old", 1, head="active")
    unused, _ = world.workload(revision, run=RunState.STOPPED)
    named, named_run = world.workload(revision, run=RunState.STOPPED)
    running, _ = world.workload(revision, run=RunState.RUNNING)
    for installation in (unused, named, running):
        _size(world, installation, 80 * GIB)
    application_id = _application(
        world, {}, state=LifecycleState.RUNNING, canonical=True
    )
    with world.sessions.begin() as session:
        application = _required(session.get(FleetProfileApplication, application_id))
        plan = FleetProfilePreview.model_validate_json(json.dumps(application.plan))
        plan.scope.node_ids = [NODE, other]
        application.plan = plan.model_dump(mode="json")
        # Exact reference in the durable progress, before another child starts.
        application.progress = FleetProfileApplicationProgress(
            effects=[
                FleetProfileEffectProgress(
                    effect_id="copy",
                    application_id=application_id,
                    plan_digest=plan.plan_digest,
                    workload_intent_ordinal=1,
                    queue_index=0,
                    kind=ProfileSwitchChildKind.RUN.value,
                    target_id=named_run,
                    node_ids=[NODE, other],
                    request_key=str(uuid.uuid4()),
                    state=ProfileEffectState.PENDING.value,
                )
            ]
        ).model_dump(mode="json")
        if hold_source == "job":
            application.state = LifecycleState.SUCCEEDED
            session.add(
                Job(
                    request_id=str(uuid.uuid4()),
                    kind=OperationKind.RECIPE_INSTALL.value,
                    state=LifecycleState.RUNNING,
                    actor="test",
                    authority_revision="r",
                    targets=[NODE, other],
                    payload_digest="0" * 64,
                    payload=RecipeInstallParent(
                        schema_version=1,
                        owner_kind="installation",
                        owner_id=named,
                        plan_digest="1" * 64,
                    ).model_dump(mode="json"),
                    current_attempt=1,
                    created_at=OLD,
                    updated_at=NOW,
                )
            )
    lifecycle = FakeLifecycle(world.sessions, apply=True)
    collector = _collector(world, lifecycle)
    with world.sessions() as session:
        capacity = spark_eviction_capacity(session, NODE, NOW)
    assert capacity.freeable == 80 * GIB
    assert (
        _required(
            collector.relief_for_spark(
                NODE,
                50 * GIB,
                source="profile-load",
                subject="second-load",
                reason="disk",
            )
        ).code
        == STORAGE_EVICTING
    )
    assert collector.collect().installations == 1
    assert lifecycle.removed == [unused]
    _report_disk(world.sessions, 81 * GIB, at=NOW + timedelta(seconds=1))
    assert (
        collector.relief_for_spark(
            NODE, 50 * GIB, source="profile-load", subject="second-load", reason="disk"
        )
        is None
    )
    with world.sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, named)).state
            == InstallationState.INSTALLED
        )
        assert (
            _required(session.get(RecipeInstallation, running)).state
            == InstallationState.INSTALLED
        )
        assert _required(
            session.get(FleetProfileApplication, application_id)
        ).state == (
            LifecycleState.RUNNING
            if hold_source == "application"
            else LifecycleState.SUCCEEDED
        )


def test_installation_content_references_hold_but_provenance_does_not(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Catches keeping only UUIDs, or treating recipe/build provenance as use."""

    sessions, _, installation_id, _ = _real_lifecycle(tmp_path, engine=postgres_engine)
    with sessions() as session:
        installation = _required(session.get(RecipeInstallation, installation_id))
        plan = parse_stored_installation_plan(installation.plan)
        compiled = next(iter(plan.compiled_execution_plans.values()))
        evidence = replace(_Evidence.read(session, _REAL_CLOCK), operations=frozenset())
        assert _installation_kept(session, installation_id, evidence) is None
        identities = (
            installation.plan_digest,
            compiled.identity.model_artifact_set_sha256,
            compiled.runtime_image.image_digest.removeprefix("sha256:"),
            compiled.runtime_image.oci_layout_sha256,
            compiled.artifacts[0].model.content_sha256,
        )
        for identity in identities:
            referenced = replace(evidence, operations=frozenset((identity,)))
            assert _installation_kept(session, installation_id, referenced) is not None
        for provenance in (
            installation.recipe_revision_id,
            installation.recipe_build_id,
        ):
            assert provenance is not None
            unreferenced = replace(evidence, operations=frozenset((provenance,)))
            assert _installation_kept(session, installation_id, unreferenced) is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_pressure_promises_profile_pointed_spark_objects_but_not_live_ones(
    world: Catalog,
) -> None:
    """Saved profiles pin the NAS cache, not Spark copies (the agent frees
    them); only a live operation keeps a model's objects on the Spark."""

    _hard_linked_models(world)

    def freeable(**changes: frozenset[str]) -> int:
        with world.sessions() as session:
            evidence = replace(_Evidence.read(session, world.now), **changes)
            items = UnusedStorageCollector._spark_items(
                session, NODE, evidence, Counter()
            )
        return sum(item.freeable for item in items)

    # Pointed or pinned by a profile: everything is still freeable.
    assert freeable(pointed_models=frozenset({"1" * 64})) == 150 * GIB
    # A live operation names M3, which is not installed but shares file B with
    # the idle M1 and M2: B stays on the Spark, only A, C and own bytes go.
    m3 = "3" * 64
    _model_objects(world, m3, {"b" * 64: 60 * GIB})
    assert freeable(operations=frozenset({m3})) == 90 * GIB


def test_shared_file_does_not_pin_an_unrelated_installation(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Fault: a pending install uses a file also linked by an unused install.

    Catches retaining all of its gigabytes for one shared tokenizer file.
    Exact installation and aggregate model/image references still retain use.
    """

    sessions, _, installation_id, nodes = _real_lifecycle(
        tmp_path, engine=postgres_engine
    )
    other_id = str(uuid.uuid4())
    with sessions.begin() as session:
        installation = _required(session.get(RecipeInstallation, installation_id))
        plan = parse_stored_installation_plan(installation.plan)
        compiled = next(iter(plan.compiled_execution_plans.values())).model_copy(
            deep=True
        )
        model = _required(installation.model_content_sha256)
        objects = {
            artifact.sha256: artifact.size_bytes for artifact in compiled.artifacts
        }
        manifest = ArtifactSetManifest(
            model_content_sha256=model,
            recipe_revision_sha256=None,
            model_content_digests=(model,),
            artifacts=tuple(
                ArtifactSpec(
                    key=artifact.file_id,
                    artifact_id=artifact.file_id,
                    path=artifact.path,
                    kind="huggingface.file",
                    repository=None,
                    source="https://huggingface.co/test/model",
                    revision=None,
                    sha256=artifact.sha256,
                    expected_bytes=artifact.size_bytes,
                    roles=tuple(artifact.roles),
                    model_content_sha256=model,
                )
                for artifact in compiled.artifacts
            ),
        )
        ModelCacheService(sessions, tmp_path / "model-cache")._ensure_set(
            session, manifest
        )
        # The installation runs that cached set: install admission records the
        # set the plan runs, the same content key the cache stores it under.
        installation.model_artifact_set_sha256 = manifest.digest
        node = _required(
            session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        node.installed_bytes = sum(objects.values()) + 1000
        # A different model selection and image, with one byte-identical file.
        compiled.identity.model_artifact_set_sha256 = "8" * 64
        compiled.runtime_image.image_digest = "sha256:" + "9" * 64
        compiled.runtime_image.oci_layout_sha256 = "a" * 64
        for artifact in compiled.artifacts:
            artifact.model.content_sha256 = "b" * 64
        payload = RecipeInstallPayload(
            installation_id=other_id,
            plan_digest="c" * 64,
            expected_bytes=100,
            compiled_execution_plan=compiled,
        )
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind=OperationKind.RECIPE_INSTALL.value,
                state=LifecycleState.RUNNING,
                actor="test",
                authority_revision="r",
                targets=list(nodes),
                payload_digest="0" * 64,
                payload=RecipeInstallParent(
                    schema_version=1,
                    owner_kind="installation",
                    owner_id=other_id,
                    plan_digest=payload.plan_digest,
                    phases=[
                        [
                            InstallPhaseOperation(
                                operation_id=str(uuid.uuid4()),
                                node_id=nodes[0],
                                payload=payload,
                            )
                        ]
                    ],
                ).model_dump(mode="json"),
                current_attempt=1,
                created_at=OLD,
                updated_at=NOW,
            )
        )
    with sessions() as session:
        from vonk_control.unused_storage_collection.references import _installation_kept

        evidence = _Evidence.read(session, _REAL_CLOCK)
        assert compiled.artifacts[0].sha256 in evidence.operations
        assert _installation_kept(session, installation_id, evidence) is None
        # Only private bytes can be promised. The live operation still pins
        # the shared object in the native store after the uninstall drops links.
        assert spark_eviction_capacity(session, nodes[0], _REAL_CLOCK).freeable == 1000


def _wait_for_download(service: ModelCacheService, tmp_path: Path):
    return _wait_for_download_of(service, tmp_path, 500)


def _touch_set(world: Catalog, set_digest: str, when: datetime) -> None:
    with world.sessions.begin() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        row.last_accessed_at = row.updated_at = when


def _wait_for_download_of(service: ModelCacheService, tmp_path: Path, size: int):
    other = tmp_path / "other"
    other.mkdir()
    return _download(
        service,
        [_artifact(other, b"n" * size, model_content_sha256="b" * 64)],
        model_content_sha256="b" * 64,
        request_key=str(uuid.uuid4()),
    )


def test_the_reserve_is_a_tenth_of_the_volume(tmp_path: Path, monkeypatch) -> None:
    """Catches a fixed 10 GiB reserve on a 10 TB volume shared with PostgreSQL."""

    volume = _Volume(tmp_path / "cache", 10_000)
    monkeypatch.setattr("shutil.disk_usage", volume.usage)
    engine = create_engine(f"sqlite:///{tmp_path / 'c.sqlite'}")
    Base.metadata.create_all(engine)
    service = ModelCacheService(sessionmaker(engine), volume.root)
    try:
        volume.external = 4000
        summary = service.storage_summary()
        assert summary.reserve_bytes == 1000
        assert service.free_bytes() == 5000
        volume.total = 100_000
        assert service.storage_summary().reserve_bytes == 10_000
        # The same line the collector keeps: one number owns both.
        assert FREE_SPACE_FOR_COLOCATED_SERVICES_FRACTION == STORAGE_LOW_FREE_FRACTION
    finally:
        service.close()


def test_a_download_that_would_breach_the_reserve_evicts_unused_models_then_runs(
    squeezed, tmp_path: Path
) -> None:
    """Catches a download admitted into the reserve, or one that waits while
    unused models sit on the volume."""

    world, service, demands = squeezed.world, squeezed.service, squeezed.demands
    waiting = _wait_for_download(service, tmp_path)
    assert waiting.failure is not None
    detail = str(waiting.failure["detail"])
    assert waiting.failure["code"] == ModelCacheCode.DOWNLOAD_BLOCKED
    assert "insufficient-reserved-storage" in detail
    assert "needs 500 bytes free beyond the 1000-byte reserve" in detail
    (demand,) = demands.active()
    assert (demand.scope, demand.required_free_bytes) == (NAS_MODELS, 1500)
    assert (
        service._claim_operations(limit=1, respect_backoff=False, transfer_slots=1)
        == []
    )

    assert squeezed.collector.collect().models == 1
    _settle(service)

    assert not _set_exists(world, squeezed.set_digest)
    claimed = service._claim_operations(
        limit=1, respect_backoff=False, transfer_slots=1
    )
    assert [operation for operation, _kind in claimed] == [waiting.id]


@pytest.mark.parametrize("protection", ["profile", "workload"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_a_download_waits_with_its_reason_when_only_protected_models_could_be_freed(
    squeezed, tmp_path: Path, protection: str
) -> None:
    """Catches evicting a profile's or a running workload's model for a download,
    and a wait that vanishes or ends when nothing can be reclaimed."""

    world, service, demands = squeezed.world, squeezed.service, squeezed.demands
    set_digest = squeezed.set_digest
    if protection == "profile":
        _head_recipe_naming(world, A_MODEL)
        _profile(world, "vonk-forge/glm")
    else:
        old = world.revision("old", 1)
        world.revision("old", 2, head="active")
        installation, _ = world.workload(old, run="stopped")
        with world.sessions.begin() as session:
            row = session.get(RecipeInstallation, installation)
            assert row is not None
            row.plan = {"model": A_MODEL}
    waiting = _wait_for_download(service, tmp_path)

    swept = squeezed.collector.collect()

    assert swept.models == 0
    assert _set_exists(world, set_digest)
    assert _scope(swept, "nas")["outcome"] == "insufficient_after_eviction"
    view = service.get_operation(waiting.id)
    assert view.failure is not None
    assert view.failure["code"] == ModelCacheCode.DOWNLOAD_BLOCKED
    assert view.failure["retryable"] is True
    assert view.next_attempt_at is not None
    assert demands.active()
    assert (
        service._claim_operations(limit=1, respect_backoff=False, transfer_slots=1)
        == []
    )


def test_unused_models_go_when_free_space_falls_below_the_reserve_with_no_download(
    squeezed,
) -> None:
    """Catches reclaiming only on a refused download: backups, builds and database
    growth consume the volume with no download pending."""

    world, service, volume = squeezed.world, squeezed.service, squeezed.volume
    collector, set_digest = squeezed.collector, squeezed.set_digest
    assert collector.tick() is False  # exactly the reserve free: nothing to do
    assert _set_exists(world, set_digest)
    volume.external += 100  # something else takes 100 bytes: below the reserve

    world.now += timedelta(seconds=3600)
    assert collector.tick() is True

    _settle(service)
    assert not _set_exists(world, set_digest)
    assert volume.free > service.reserve_of(volume.total)


def test_eviction_goes_toward_the_line_when_a_waiting_download_cannot_be_met(
    squeezed, tmp_path: Path
) -> None:
    """Catches all-or-nothing eviction: the 10 % line sits far above what unused
    models can free, so while any download waited nothing was evicted and the
    co-located database stayed at about 1 % free."""

    world, service = squeezed.world, squeezed.service
    squeezed.volume.fill_until_free(300)  # line and reserve 1000; model frees 1000
    waiting = _wait_for_download(service, tmp_path)  # 500 + 1000 > 300 + 1000
    assert squeezed.demands.active()

    swept = squeezed.collector.collect()

    assert swept.models == 1
    # Evicted what it could; only the remainder of the demand is insufficient.
    assert _scope(swept, "nas")["outcome"] == "insufficient_after_eviction"
    _settle(service)
    assert not _set_exists(world, squeezed.set_digest)
    assert squeezed.volume.free >= 1300
    # Still short of the whole demand (500 + 1000): the download keeps waiting.
    assert service.get_operation(waiting.id).failure is not None


def test_an_unmet_demand_evicts_only_as_far_as_the_line(
    world: Catalog, tmp_path: Path, monkeypatch
) -> None:
    """Catches evicting every unused model for a download that cannot be met:
    the cached models would go and the download would still wait."""

    volume = _Volume(tmp_path / "nas-cache", 10_000)
    monkeypatch.setattr("shutil.disk_usage", volume.usage)
    service = ModelCacheService(world.sessions, volume.root, fixture_sources=True)
    demands = StorageDemands(lambda: datetime.now(UTC))
    service.bind_storage_demands(demands)
    try:
        digests = []
        for index in range(3):
            source = tmp_path / f"src{index}"
            source.mkdir()
            model = f"{index + 1:064x}"
            stored = _download(
                service,
                [
                    _artifact(
                        source, bytes([index + 1]) * 1000, model_content_sha256=model
                    )
                ],
                model_content_sha256=model,
                request_key=str(uuid.uuid4()),
            )
            assert stored.state == "succeeded"
            digests.append(str(stored.artifact_set_sha256))
            _touch_set(world, digests[-1], NOW - timedelta(days=9 - index))
        volume.fill_until_free(999)  # one byte under the 1000-byte line
        waiting = _wait_for_download_of(service, tmp_path, 5000)
        assert waiting.failure is not None
        collector = _models(
            world,
            service,
            demands=demands,
            disk_usage=volume.pair,
            reserve_fraction=0,
            reserve_floor_bytes=0,
            # The line stays at a tenth although the waiting set is larger.
            low_free_cap_fraction=0.1,
        )

        swept = collector.collect()

        _settle(service)
        assert swept.models == 1
        assert _scope(swept, "nas")["outcome"] == "insufficient_after_eviction"
        assert [_set_exists(world, digest) for digest in digests] == [False, True, True]
    finally:
        service.close()


def test_an_unmet_spark_demand_uninstalls_only_as_far_as_the_line(
    world: Catalog,
) -> None:
    """Catches uninstalling every unused installation for a load that cannot be
    met by all of them together."""

    oldest, _middle, _newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions)
    demands = StorageDemands(lambda: world.now)
    _within_grace(world)
    collector = _collector(world, lifecycle, free=90 * GIB, demands=demands)
    demands.request(
        spark_scope(NODE),
        900 * GIB,
        source="profile-load",
        subject="p",
        reason="run-switch.insufficient-disk",
    )

    result = collector.collect()

    assert lifecycle.removed == [oldest]
    assert _scope(result, spark_scope(NODE))["outcome"] == "insufficient_after_eviction"
    relief = collector.relief_for_spark(
        NODE, 900 * GIB, source="profile-load", subject="p", reason="x"
    )
    assert relief is not None
    assert relief.code == STORAGE_INSUFFICIENT


def _inside_window(world: Catalog, service: ModelCacheService, **kwargs):
    """A collector whose clock is an hour after the cached model was written, so
    the last-day window is in play (``_models`` sits past it and hides it)."""

    return _collector(
        world,
        model_cache=service,
        model_cache_root=service._root,
        now=datetime.now(UTC) + timedelta(hours=1),
        **kwargs,
    )


def test_a_model_a_finished_operation_named_an_hour_ago_is_evicted_under_pressure(
    world: Catalog, cached
) -> None:
    """Catches a finished download, load or job in the last day keeping a model
    as 'live operation' while the removal review reports no blockers."""

    service, _ = cached
    assert not service.review_model_removal(A_MODEL).blockers

    result = _inside_window(world, service).collect()

    assert result.models == 1, result.kept
    assert _kept(result, "live operation") == 0


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_image_a_run_that_ended_an_hour_ago_named_is_not_expired(
    world: Catalog, tmp_path: Path
) -> None:
    """An old receipt whose archive a finished run named an hour ago has no
    reference but its recency: the expiry pass must not remove it."""

    path = _receipt(tmp_path, AN_IMAGE)
    free = TOTAL // 2
    result = _collector(world, image_cache_root=tmp_path, free=free).collect()
    assert not path.exists()
    assert result.images == 1

    path = _receipt(tmp_path, AN_IMAGE)
    world.job(
        {"plan": {"image": AN_IMAGE}},
        state="succeeded",
        updated=NOW - timedelta(hours=1),
    )
    result = _collector(world, image_cache_root=tmp_path, free=free).collect()
    assert path.exists()
    assert result.images == 0


def _world_none(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    del world, set_digest


def _world_saved_profile(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    del set_digest
    recipe = _head_recipe_naming(world, A_MODEL)
    # The catalog binds an active recipe revision to the model revisions it
    # names by relational rows; the removal review reads those, not the JSON.
    model = world.revision("glm-weights", 1, kind="model")
    with world.sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == model)
            .values(content_digest=A_MODEL)
        )
        session.add(
            CatalogRecipeModelReference(
                recipe_revision_id=recipe,
                recipe_kind="recipe",
                selection_id="weights",
                model_revision_id=model,
                model_kind="model",
                model_publisher="vonk-forge",
                model_slug="glm-weights",
                model_content_digest=A_MODEL,
            )
        )
    _profile(world, "vonk-forge/glm")


_OTHER_IMAGE = "sha256:" + "e" * 64


def _world_active_installation(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    """A readable installation plan that runs the cached set, with a run live."""

    del tmp_path
    installation = _installation(world, set_digest)
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, installation)
        assert row is not None
        # Catalog.workload's image digest equals A_MODEL's hex: keep them apart.
        plan = dict(row.plan)
        plan["image_digest"] = _OTHER_IMAGE
        row.image_digest = _OTHER_IMAGE
        row.plan = plan
        run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == installation)
        )
        assert run is not None
        run.state = "running"


def _switch_job(world: Catalog, set_digest: str, tmp_path: Path, *, state: str) -> None:
    """The accepted Run/Switch job a real planner stores, naming the cached set."""

    engine = create_engine(f"sqlite:///{tmp_path / 'planner.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions, _planner, parent, _request, _selected = _direct_parent(tmp_path, engine)
    with sessions() as session:
        source = session.get(Job, parent.operation_id)
        assert source is not None
        payload: dict[str, Any] = copy.deepcopy(source.payload)  # type: ignore[assignment]
        kind, targets = source.kind, list(source.targets)
        authority, digest = source.authority_revision, source.payload_digest
    payload["plan"]["storage"]["artifact_set_sha256"] = set_digest
    stamp = datetime.now(UTC)
    with world.sessions.begin() as session:
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind=kind,
                state=state,
                actor="test",
                authority_revision=authority,
                targets=targets,
                payload_digest=digest,
                payload=payload,
                current_attempt=0,
                created_at=stamp,
                updated_at=stamp,
            )
        )


def _world_live_job(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    _switch_job(world, set_digest, tmp_path, state="running")


def _world_job_ended_an_hour_ago(
    world: Catalog, set_digest: str, tmp_path: Path
) -> None:
    _switch_job(world, set_digest, tmp_path, state="succeeded")


def _world_held_assignment(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    stamp = datetime.now(UTC)
    with world.sessions.begin() as session:
        session.add(
            ArtifactDistributionAssignment(
                plan_digest="3" * 64,
                node_id=NODE,
                generation=1,
                expires_at=stamp + timedelta(hours=6),
                model_artifact_set_sha256=set_digest,
                objects=[],
                oci_image_digest="sha256:" + "5" * 64,
                oci_image_config_digest="sha256:" + "6" * 64,
                oci_archive_sha256="2" * 64,
                state="active",
                created_at=stamp,
                updated_at=stamp,
            )
        )


def _world_live_download(world: Catalog, set_digest: str, tmp_path: Path) -> None:
    del set_digest
    with world.sessions.begin() as session:
        for row in session.scalars(select(ModelCacheOperation)):
            row.state = "running"


@pytest.mark.parametrize(
    "build",
    [
        _world_none,
        _world_saved_profile,
        _world_active_installation,
        _world_live_job,
        _world_job_ended_an_hour_ago,
        _world_held_assignment,
        _world_live_download,
    ],
)
def test_under_pressure_the_collector_never_keeps_a_model_the_review_shows_clear(
    world: Catalog,
    cached,
    tmp_path: Path,
    build: Callable[[Catalog, str, Path], None],
) -> None:
    """Catches the collector keeping a model while the removal review shows no
    blockers (recency was one such reason). The other direction, removing what
    the review blocks, is stopped by the removal's own verify, which runs the
    review; here it shows as nothing removed."""

    service, set_digest = cached
    build(world, set_digest, tmp_path)
    blocked = bool(service.review_model_removal(A_MODEL).blockers)

    result = _inside_window(world, service).collect()

    assert (result.models == 0) == blocked, (result.kept, blocked)


def test_a_pass_scans_operations_once_for_what_is_live_and_once_for_ordering(
    world: Catalog, cached, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the 24 hour operation scan, or the live scan, repeating per
    removal or per consumer: the pass reads live operations once, the last day
    once (ordering only), and a removal's verify re-reads live operations once
    and never the last day."""

    calls: list[bool] = []
    original = collector_evidence.operation_tokens

    def counted(*args, **kwargs):
        calls.append(kwargs.get("recent", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(collector_evidence, "operation_tokens", counted)
    service, _ = cached

    result = _inside_window(world, service).collect()

    assert result.models == 1, result.kept
    # One live and one last-day scan for the pass, one live scan for the verify.
    assert calls.count(True) == 1, calls
    assert calls.count(False) == 2, calls


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_image_a_run_that_ended_an_hour_ago_named_is_evicted_last_under_pressure(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches recent use keeping an image, or not ordering it: short by a byte
    the idle receipt goes and the recently used one stays; short by more and the
    recently used one goes too."""

    recent = _receipt(tmp_path, "1" * 64)
    idle = _receipt(tmp_path, "7" * 64)
    world.job(
        {"plan": {"image": "1" * 64}},
        state="succeeded",
        updated=NOW - timedelta(hours=1),
    )
    exact = {"reserve_floor_bytes": 0, "reserve_fraction": 0.0}

    result = _collector(
        world, image_cache_root=tmp_path, free=100 * GIB - 1, **exact
    ).collect()
    assert recent.exists()
    assert not idle.exists()
    assert result.images == 1
    assert _kept(result, "live operation") == 0

    result = _collector(world, image_cache_root=tmp_path, free=GIB, **exact).collect()
    assert not recent.exists()
    assert result.images == 1
