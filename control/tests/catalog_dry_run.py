"""Every catalog recipe through the Controller's offline pipeline, no Spark.

The canonical recipe library the suite runs against (the reviewed revision CI
checks out and builds: the bytes its signed release publishes) is imported
through the production catalog sync into a disposable SQLite catalog. Per
recipe, the production code then does what precedes any Spark: resolve the
model-cache manifest, compile every rank, describe the distribution objects,
build each node's assignment and route the gateway.

Only two facts are assumed: the NAS holds every model object (ingress verified
them) and the runtime image is built. Everything derived from them is the
Controller's own output.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import CompiledExecutionPlan, DistributionObject
from vonk_control import recipe_packages
from vonk_control.auth import CursorCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
    reviewed_catalog_content,
)
from vonk_control.distribution.model_cache import ModelCacheObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.distribution_executor.identity import DistributionIdentity
from vonk_control.distribution_executor.receipts import RuntimeImagePull
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.model_cache import ArtifactSetManifest, ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocumentRevision,
    ClusterMappingNode,
    RecipeBuild,
)
from vonk_control.run_switch_contract import RunSwitchOperationResult, RunSwitchPlan
from vonk_control.runtime_image_preparation.contracts import RuntimeImageReceipt
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import read_recipe

from .signed_recipe_release import SignedRecipeRelease
from .signed_recipe_release import _verify as _test_signature

NOW = datetime(2026, 10, 12, tzinfo=UTC)
IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE = RuntimeImagePull(
    image_digest=IMAGE_DIGEST,
    config_digest="sha256:" + "e" * 64,
    address="f" * 64,
)


def catalog_index(root: Path) -> dict[str, Any]:
    return json.loads((root / "catalog-index.json").read_text(encoding="utf-8"))


def indexed_recipes(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every indexed recipe row (document, content digest, package) by slug."""

    recipes = index["recipes"]
    assert isinstance(recipes, list) and recipes
    return {row["document"]["identity"]["slug"]: row for row in recipes}


class _NasHoldsEveryObject(ModelCacheService):
    """The NAS cache with every object verified at ingress; nothing downloads."""

    def _managed_cached_objects(self, manifest: ArtifactSetManifest) -> frozenset[str]:
        return frozenset(spec.sha256 for spec in manifest.artifacts)


def _node_id(rank: int) -> str:
    return "spk_" + hashlib.sha256(f"spark-{rank}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Transfer:
    """What the distribution executor sends for one recipe's model set."""

    objects: tuple[DistributionObject, ...]
    artifact_set_sha256: str
    total_bytes: int


class CatalogDryRun:
    """The production pipeline over one imported catalog, cached per recipe."""

    def __init__(self, sessions: sessionmaker[Session], cache_root: Path) -> None:
        self._sessions = sessions
        self._cache = _NasHoldsEveryObject(sessions, cache_root, clock=lambda: NOW)
        self._identity = DistributionIdentity(
            sessions,
            None,  # type: ignore[arg-type] - assignments need no agent jobs
            SimpleNamespace(source=ModelCacheObjectSource.from_service(self._cache)),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )

    def _revision(self, session: Session, digest: str) -> CatalogDocumentRevision:
        row = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
                CatalogDocumentRevision.content_digest == digest,
            )
        )
        assert row is not None, "the catalog sync did not import this recipe"
        return row

    def revision(self, digest: str) -> CatalogDocumentRevision:
        with self._sessions() as session:
            return self._revision(session, digest)

    @cache  # noqa: B019 - one instance per test session
    def manifest(self, digest: str) -> ArtifactSetManifest:
        """The recipe's model set, recorded as cached like a completed download."""

        manifest = self._cache.resolve_artifact_set(recipe_revision_sha256=digest)
        self._cache.adopt_verified_set(manifest)
        return manifest

    @cache  # noqa: B019 - one instance per test session
    def roles(self, digest: str) -> dict[str, str]:
        """A fake fleet: one Spark per rank of the topology, by node ID."""

        recipe = read_recipe(self.revision(digest).document)
        names = [role.name for role in recipe.topology.roles for _ in range(role.count)]
        return {_node_id(rank): name for rank, name in enumerate(names)}

    @cache  # noqa: B019 - one instance per test session
    def plans(self, digest: str) -> dict[str, CompiledExecutionPlan]:
        """One compiled launch plan per Spark of the recipe's whole topology."""

        with self._sessions() as session:
            revision = self._revision(session, digest)
            nodes = [
                ClusterMappingNode(node_id=node_id, rank=rank, role=role)
                for rank, (node_id, role) in enumerate(self.roles(digest).items())
            ]
            build = RecipeBuild(
                id="00000000-0000-4000-8000-000000000001",
                recipe_revision_id=revision.id,
                builder_node_id=_node_id(0),
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=IMAGE_DIGEST,
                created_at=NOW,
                updated_at=NOW,
            )
            return ControllerExecutionPlanService(
                self._cache, runtime_image_resolver=_built_image
            ).compile_installation(
                session,
                revision=revision,
                build=build,
                mapping_nodes=nodes,
                parameters=None,
            )

    def _plan(self, manifest: ArtifactSetManifest) -> RunSwitchPlan:
        """The run/switch plan fields distribution reads, as preparation sets them."""

        model = SimpleNamespace(
            artifact_set_sha256=manifest.digest,
            artifact_set_bytes=manifest.expected_bytes,
        )
        digests = list({item.sha256: None for item in manifest.artifacts})
        return cast(
            "RunSwitchPlan",
            SimpleNamespace(
                plan_digest="c" * 64,
                mapping=None,
                preparation=SimpleNamespace(model=model),
                storage=SimpleNamespace(artifact_digests=digests),
            ),
        )

    def transfer(self, digest: str) -> Transfer:
        plan = self._plan(self.manifest(digest))
        progress = cast("RunSwitchOperationResult", SimpleNamespace(phase_results=[]))
        objects, set_digest, total = self._identity._model_objects(plan, progress)
        return Transfer(objects, set_digest, total)

    def assignments(self, digest: str) -> list[NodeDistributionAssignment]:
        """The assignment each Spark of the topology receives."""

        transfer = self.transfer(digest)
        plan = self._plan(self.manifest(digest))
        return [
            self._identity._assignment(
                plan,
                node_id,
                transfer.objects,
                IMAGE,
                model_set_digest=transfer.artifact_set_sha256,
            )
            for node_id in self.plans(digest)
        ]


def _built_image(
    document: object, image_digest: str, runtime_spec: object
) -> RuntimeImageReceipt:
    del document, runtime_spec
    return RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher="vonk",
        distribution_slug="catalog-dry-run",
        distribution_content_sha256="c" * 64,
        image_digest=image_digest,
        oci_archive_sha256=IMAGE.address,
        image_bytes=4096,
        local_image_config_id=IMAGE.config_digest,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        archive_path="/managed/catalog-dry-run",
        recorded_at=NOW.isoformat(),
        build_id="catalog-dry-run",
        runtime_adapter="catalog-dry-run",
        runtime_adapter_sha256="a" * 64,
    )


def import_catalog(root: Path, work: Path) -> Iterator[CatalogDryRun]:
    """Sync the built library into SQLite through the production reader and sync."""

    engine = create_engine(f"sqlite:///{work / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock: Callable[[], datetime] = lambda: NOW
    catalog = CatalogService(
        sessions,
        clock=clock,
        cursors=CursorCodec(b"c" * 32),
        source_bundles=SourceBundleStore(work / "source-bundles"),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recipe_packages, "verify_release_checksums", _test_signature)
        reader = SignedRecipeRelease.from_library(catalog_index(root), root).client(
            work / "packages"
        )
        snapshot = reader.list()
        reader.prepare(snapshot)
        ManagedRecipeCatalogSyncService(
            sessions, catalog=catalog, reader=reader, clock=clock
        ).sync(
            ManagedCatalogSyncRequest(
                request_key="00000000-0000-4000-8000-000000000094",
                trigger=CatalogSyncTrigger.MANUAL,
                actor="system:catalog-dry-run",
                reviewed_content_sha256=reviewed_catalog_content(snapshot),
            )
        )
        reader.close()
    try:
        yield CatalogDryRun(sessions, work / "model-cache")
    finally:
        engine.dispose()
