"""Shared helpers for the unused_storage_collection tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from vonk_control.catalog.revision_collection import GRACE
from vonk_control.fleet_profile_contract.review import FleetProfilePreview
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base
from vonk_control.models.cache import (
    ModelCacheSet,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.models.fleet import (
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
)
from vonk_control.models.operations import (
    AgentOperation,
)
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.recipe_operations.errors import RecipeOperationConflict
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.storage_demands import (
    StorageDemands,
)
from vonk_control.unused_storage_collection.common import ACTOR
from vonk_control.unused_storage_collection.service import UnusedStorageCollector

from .model_cache_support import _artifact, _download
from .removal_settle import advance_until_ended
from .runtime_image_fixtures import place_test_image
from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog

A_MODEL = "a" * 64


AN_IMAGE = "d" * 64


GIB = 1024**3


TOTAL = 1000 * GIB


@pytest.fixture
def world(tmp_path: Path) -> Catalog:
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Catalog(engine)


class FakeLifecycle:
    """Records the uninstalls the sweep asks for, running its guard as the real
    one does: in a transaction, before anything is queued."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        between: Callable[[str], None] | None = None,
        refuse: frozenset[str] = frozenset(),
        apply: bool = False,
    ) -> None:
        self._sessions = sessions
        self._between = between
        self._refuse = refuse
        self._apply = apply
        self.removed: list[str] = []
        self.stopped: list[str] = []
        self.also_removing: dict[str, tuple[str, ...]] = {}

    def preview_stop(self, run_id: str):
        return SimpleNamespace(
            allowed=True, blockers=(), plan_digest="e" * 64, target_node_ids=(NODE,)
        )

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> None:
        # Joining the Sparks' intent is what keeps an unattended Stop from
        # superseding anything.
        assert actor == ACTOR and workload_intent_ordinal is not None
        self.stopped.append(run_id)

    def uninstall(
        self,
        installation_id: str,
        *,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing=(),
        preserve_model_files=False,
    ) -> None:
        assert actor == ACTOR and unattended_guard is not None
        assert preserve_model_files
        # The sweep has judged the installation unused; a load can still
        # arrive before the removal is queued.
        if self._between is not None:
            self._between(installation_id)
        self.also_removing[installation_id] = tuple(sorted(also_removing))
        if installation_id in self._refuse:
            raise RecipeOperationConflict("refused")
        with self._sessions.begin() as session:
            unattended_guard(session)
            if self._apply:
                installation = session.get(RecipeInstallation, installation_id)
                assert installation is not None
                installation.state = "uninstalled"
        self.removed.append(installation_id)


def _report_disk(
    sessions: sessionmaker[Session],
    free: int,
    *,
    at: datetime,
    node: str = NODE,
    total: int = TOTAL,
    unused_models: int | None = None,
) -> None:
    """What a Spark's inventory reports for its disk (and its unused model
    files, when its agent reports them), as of ``at``."""

    stamp = at
    with sessions.begin() as session:
        existing = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == node,
                NodeInventorySnapshot.observed_at == stamp,
            )
        )
        if existing is not None:
            existing.disk_free_bytes = free
            existing.unused_model_bytes = unused_models
            return
        session.add(
            NodeInventorySnapshot(
                node_id=node,
                observed_at=stamp,
                received_at=stamp,
                disk_total_bytes=total,
                disk_free_bytes=free,
                host_memory_total_bytes=1,
                host_memory_free_bytes=1,
                gpu_memory_total_bytes=1,
                gpu_memory_free_bytes=1,
                gpu_count=1,
                memory_pool="separate",
                artifact_store_read_only=False,
                capabilities=[],
                unused_model_bytes=unused_models,
                evidence_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            )
        )


def _collector(
    world: Catalog,
    lifecycle: FakeLifecycle | None = None,
    *,
    now: datetime | None = None,
    free: int = GIB,
    unused_models: int | None = None,
    **options,
) -> UnusedStorageCollector:
    """A collector for a Spark and a NAS that both report ``free`` bytes of a
    1000 GiB disk: by default 1 GiB, far below the 10 % low line."""

    world.now = now or NOW
    _report_disk(world.sessions, free, at=world.now, unused_models=unused_models)
    options.setdefault("disk_usage", lambda _path: (TOTAL, free))
    if options.get("image_cache_root") is not None:
        options.setdefault(
            "images", FilesystemRuntimeImageStorage(options["image_cache_root"])
        )
    return UnusedStorageCollector(
        world.sessions,
        clock=lambda: world.now,
        lifecycle=lifecycle or FakeLifecycle(world.sessions),  # type: ignore[arg-type]
        **options,
    )


def _profile(world: Catalog, selector: str, sparks: list[str] | None = None) -> None:
    """A saved profile that assigns ``selector`` to the given Sparks."""

    with world.sessions.begin() as session:
        session.add(
            FleetProfile(
                number=1,
                name="Coding",
                installation_policy="keep-cached",
                assignments=[
                    {
                        "recipe_selector": selector,
                        "spark_ids": sparks or [NODE],
                        "desired_state": "running",
                        "option_choices": {},
                    }
                ],
                created_by="test",
                created_at=OLD,
                updated_at=OLD,
            )
        )


def _kept(result, reason: str) -> int:
    return sum(count for key, count in result.kept.items() if reason in key)


def _scope(result, scope: str) -> dict[str, object]:
    return next(item for item in result.scopes if item["scope"] == scope)


def _size(
    world: Catalog,
    installation_id: str,
    size: int,
    *,
    model: str | None = None,
    model_set: str | None = None,
) -> None:
    """What the installation holds on its Spark, and optionally its model and
    the model set its plan runs."""

    with world.sessions.begin() as session:
        for node in session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        ):
            node.installed_bytes = size
        if model is not None:
            row = session.get(RecipeInstallation, installation_id)
            assert row is not None
            row.model_content_sha256 = model
        if model_set is not None:
            row = session.get(RecipeInstallation, installation_id)
            assert row is not None
            row.model_artifact_set_sha256 = model_set


def _three_idle(world: Catalog, *, newest_age: timedelta) -> tuple[str, str, str]:
    """Three unused 80 GiB installations, last used 9, 5 and ``newest_age`` ago."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    found = []
    for age in (timedelta(days=9), timedelta(days=5), newest_age):
        installation, _ = world.workload(old, run="stopped", touched=NOW - age)
        _size(world, installation, 80 * GIB)
        found.append(installation)
    return found[0], found[1], found[2]


def _within_grace(world: Catalog) -> None:
    """Keep pressure tests inside the independent age grace, preserving LRU order."""
    with world.sessions.begin() as session:
        for model in (RecipeInstallation, InstallationNode, RecipeRun):
            for row in session.scalars(select(model)):
                from vonk_control.unused_storage_collection.references import _utc

                row.updated_at = NOW - (NOW - _utc(row.updated_at)) / 100


def _model_objects(world: Catalog, model: str, objects: dict[str, int]) -> str:
    """A cached model set first requested by ``model`` holding these files
    (file digest -> bytes); its set digest, which installations record."""

    set_digest = uuid.uuid4().hex + uuid.uuid4().hex
    manifest = {
        "schema_version": 2,
        "source_policy": "nas-first",
        "model_content_sha256": model,
        "recipe_revision_sha256": None,
        "model_definition_ref": None,
        "model_content_digests": [model],
        "artifacts": [
            {
                "key": digest[:8],
                "id": digest[:8],
                "path": f"{digest[:8]}.bin",
                "kind": "file",
                "repository": None,
                "source": "fixture",
                "revision": None,
                "sha256": digest,
                "download_bytes": size,
                "roles": ["model"],
                "model_content_sha256": model,
            }
            for digest, size in sorted(objects.items())
        ],
        "verified_models": [],
    }
    with world.sessions.begin() as session:
        session.add(
            ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=2,
                model_content_sha256=model,
                manifest=manifest,
                expected_bytes=sum(objects.values()),
                verified_bytes=sum(objects.values()),
                state="cached",
                created_at=OLD,
                updated_at=OLD,
                last_accessed_at=OLD,
            )
        )
    return set_digest


def _hard_linked_models(world: Catalog) -> tuple[list[str], list[str]]:
    """Five installations of model M1 and three of M2, which share one file.

    M1 = A (40) + B (60) and M2 = B (60) + C (30); each installation holds 10
    bytes of its own besides the linked files. All idle, M1 used less recently.
    """

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    m1, m2 = "1" * 64, "2" * 64
    s1 = _model_objects(world, m1, {"a" * 64: 40 * GIB, "b" * 64: 60 * GIB})
    s2 = _model_objects(world, m2, {"b" * 64: 60 * GIB, "c" * 64: 30 * GIB})
    first: list[str] = []
    second: list[str] = []
    for model, model_set, installed, found, age in (
        (m1, s1, 110 * GIB, first, timedelta(days=9)),
        (m1, s1, 110 * GIB, first, timedelta(days=9)),
        (m1, s1, 110 * GIB, first, timedelta(days=9)),
        (m1, s1, 110 * GIB, first, timedelta(days=9)),
        (m1, s1, 110 * GIB, first, timedelta(days=9)),
        (m2, s2, 100 * GIB, second, timedelta(days=5)),
        (m2, s2, 100 * GIB, second, timedelta(days=5)),
        (m2, s2, 100 * GIB, second, timedelta(days=5)),
    ):
        installation, _ = world.workload(old, run="stopped", touched=NOW - age)
        _size(world, installation, installed, model=model, model_set=model_set)
        found.append(installation)
    return first, second


def _application(
    world: Catalog,
    plan: dict[str, object],
    *,
    state: str,
    selected: bool = False,
    progress: dict[str, object] | None = None,
    canonical: bool = False,
    operation: str | None = None,
) -> str:
    with world.sessions.begin() as session:
        profile = session.scalar(select(FleetProfile))
        if profile is None:
            _profile(world, "vonk-forge/other")
            profile = session.scalar(select(FleetProfile))
        assert profile is not None
        plan_digest = uuid.uuid4().hex + uuid.uuid4().hex
        if canonical:
            # A readable accepted scope can wait for capacity without making
            # every obsolete installation on that Spark a live asset reference.
            plan = FleetProfilePreview.model_validate(
                {
                    "profile_id": profile.id,
                    "profile_name": "Capacity waiter",
                    "profile_digest": "1" * 64,
                    "profile_revision": None,
                    "profile_definition": None,
                    "allowed": False,
                    "scope": {"node_ids": [NODE], "idle_node_ids": []},
                    "summary": {
                        "already_correct": 0,
                        "placements": 0,
                        "builds": 0,
                        "distributions": 0,
                        "installs": 0,
                        "starts": 0,
                        "stops": 0,
                        "uninstalls": 0,
                        "blockers": 0,
                    },
                    "assignments": [],
                    "resolved_assignments": [],
                    "admission_decisions": [],
                    "preparation_decisions": [],
                    "effects": {"runs": [], "installations": [], "superseded": []},
                    "steps": [
                        {
                            "index": 0,
                            "kind": "switch",
                            "node_ids": [NODE],
                            "label": "Wait for capacity",
                        }
                    ],
                    "reasons": [],
                    "generated_at": OLD,
                    "assessments": [],
                    "plan_digest": plan_digest,
                    "effects_digest": "2" * 64,
                }
            ).model_dump(mode="json")
        application = FleetProfileApplication(
            request_key=str(uuid.uuid4()),
            profile_id=profile.id,
            profile_digest="1" * 64,
            plan_digest=plan_digest,
            state=state,
            plan=plan,
            actor="test",
            created_at=OLD,
            updated_at=OLD,
            current_operation_id=operation,
            **({"progress": progress} if progress is not None else {}),
        )
        session.add(application)
        session.flush()
        if selected:
            session.add(
                FleetProfileSelection(
                    singleton_id=1,
                    generation=1,
                    profile_id=profile.id,
                    profile_revision=1,
                    application_id=application.id,
                    roster_digest="2" * 64,
                    updated_at=OLD,
                )
            )
        return application.id


# -- the real lifecycle: no new workload intent --------------------------------------


def _real_lifecycle(tmp_path: Path, *, nodes: int = 1, engine: Engine | None = None):
    from .recipe_operations_support import installed_recipe, setup_services

    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes, engine=engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id=str(uuid.uuid4())
    )
    return sessions, service, installation.owner_id, node_ids


_REAL_CLOCK = datetime(2026, 8, 7, 12, tzinfo=UTC) + timedelta(days=2)


def _real_collector(
    sessions: sessionmaker[Session], service, nodes: tuple[str, ...]
) -> UnusedStorageCollector:
    """A collector over a real lifecycle whose Sparks report little free disk."""

    for node in nodes:
        _report_disk(sessions, GIB, at=_REAL_CLOCK, node=node)
    return UnusedStorageCollector(
        sessions, clock=lambda: _REAL_CLOCK, lifecycle=service
    )


def _required[T](value: T | None) -> T:
    assert value is not None
    return value


def _agents_succeed(sessions: sessionmaker[Session], service, job_id: str) -> None:
    """Every Spark reports success for each step of the job, phase by phase."""

    while service.get(job_id).state == "running":
        with sessions() as session:
            pending = [
                child.node_id
                for child in session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job_id,
                        AgentOperation.state == "queued",
                    )
                )
            ]
        assert pending
        for node_id in pending:
            service.record_node_result(job_id, node_id, succeeded=True, evidence={})


# -- image receipts -----------------------------------------------------------------


def _receipt(root: Path, archive: str, *, age: timedelta = timedelta(days=3)) -> Path:
    """A published receipt over a stored image of 1 KiB, last written ``age`` ago."""

    place_test_image(FilesystemRuntimeImageStorage(root), archive, 1024)
    path = root / "image-cache" / f"{archive}.receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


# -- cached models ----------------------------------------------------------------


def _head_recipe_naming(world: Catalog, model_digest: str) -> str:
    return world.revision(
        "glm",
        1,
        head="active",
        document={"models": [{"model": {"content_sha256": model_digest}}]},
    )


@pytest.fixture
def cached(tmp_path: Path, world: Catalog):
    """A real model cache over the same database, holding one model."""

    service = ModelCacheService(
        world.sessions, tmp_path / "nas-cache", free_fraction=0, fixture_sources=True
    )
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    yield service, str(downloaded.artifact_set_sha256)
    service.close()


def _models(world: Catalog, service: ModelCacheService, **kwargs):
    return _collector(
        world,
        model_cache=service,
        model_cache_root=service._root,
        now=datetime.now(UTC) + GRACE + timedelta(hours=1),
        **kwargs,
    )


def _settle(service: ModelCacheService) -> None:
    advance_until_ended(service)


def _set_exists(world: Catalog, set_digest: str) -> bool:
    with world.sessions() as session:
        return session.get(ModelCacheSet, set_digest) is not None


# -- the NAS keeps a tenth of its volume free ----------------------------------------


class _Volume:
    """A NAS volume of ``total`` bytes: the cache's own files plus what other
    services (PostgreSQL, backups, builds) hold, so deleting a model frees space."""

    def __init__(self, root: Path, total: int) -> None:
        self.root = root
        self.total = total
        self.external = 0

    def cache_used(self) -> int:
        return sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )

    @property
    def free(self) -> int:
        return self.total - self.external - self.cache_used()

    def fill_until_free(self, free: int) -> None:
        self.external = 0
        self.external = self.total - self.cache_used() - free

    def usage(self, _path: object = None):
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        return usage(self.total, self.total - self.free, self.free)

    def pair(self, _path: Path) -> tuple[int, int]:
        return self.total, self.free


@pytest.fixture
def squeezed(world: Catalog, tmp_path: Path, monkeypatch):
    """A cache holding one 1000-byte model on a 10000-byte volume that has
    exactly its reserve (a tenth) free, and a collector for that volume."""

    volume = _Volume(tmp_path / "nas-cache", 10_000)
    monkeypatch.setattr("shutil.disk_usage", volume.usage)
    service = ModelCacheService(world.sessions, volume.root, fixture_sources=True)
    demands = StorageDemands(lambda: datetime.now(UTC))
    service.bind_storage_demands(demands)
    stored = _download(
        service,
        [_artifact(tmp_path, b"w" * 1000, model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert stored.state == "succeeded"
    volume.fill_until_free(1000)
    collector = _models(
        world,
        service,
        demands=demands,
        disk_usage=volume.pair,
        reserve_fraction=0,
        reserve_floor_bytes=0,
    )
    yield SimpleNamespace(
        world=world,
        volume=volume,
        demands=demands,
        collector=collector,
        set_digest=str(stored.artifact_set_sha256),
        service=service,
    )
    service.close()
