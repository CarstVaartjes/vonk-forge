"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .auth import CursorCodec
from .library_contract import (
    _MAX_PAGE_RECIPES,
    FreshnessPolicy,
    LibraryCapabilityInventory,
    LibraryModel,
    LibraryRecipeDetail,
    LibraryRecipeList,
    LibraryRecipeSummary,
    LibrarySnapshot,
    ModelVersionIdentity,
    OperationalState,
    RecipeDiskRequirements,
    RecipeFabric,
    RecipeMemoryRequirements,
    RecipeParallelism,
    RecipeRole,
    RecipeTopology,
    _bounded_text,
    _saturating_nonnegative,
    _utc,
)
from .models import CatalogDocumentRevision


def _topology(value: Mapping[str, object]) -> RecipeTopology:
    roles: list[RecipeRole] = []
    raw_roles = value.get("roles", [])
    if not isinstance(raw_roles, list):
        raise TypeError("recipe topology roles are invalid")
    for raw_role in raw_roles:
        if not isinstance(raw_role, Mapping):
            raise TypeError("recipe topology role is invalid")
        resources = raw_role.get("resources")
        if not isinstance(resources, Mapping):
            raise TypeError("recipe topology resources are invalid")
        raw_disk = resources.get("disk")
        raw_memory = resources.get("memory")
        if not isinstance(raw_disk, Mapping) or not isinstance(raw_memory, Mapping):
            raise TypeError("recipe topology resource dimensions are invalid")
        roles.append(
            RecipeRole(
                name=_bounded_text(raw_role.get("name", ""), 64),
                count=max(1, _saturating_nonnegative(raw_role.get("count", 1))),
                endpoint_owner=bool(raw_role.get("endpoint_owner", False)),
                artifacts=[
                    _bounded_text(item, 64)
                    for item in raw_role.get("artifacts", [])
                    if isinstance(item, str)
                ][:128],
                disk=RecipeDiskRequirements(
                    **{
                        key: _saturating_nonnegative(item)
                        for key, item in raw_disk.items()
                        if key in RecipeDiskRequirements.model_fields
                    }
                ),
                memory=RecipeMemoryRequirements(
                    **{
                        key: (
                            item
                            if key == "kind"
                            else _saturating_nonnegative(item)
                        )
                        for key, item in raw_memory.items()
                        if key in RecipeMemoryRequirements.model_fields
                    }
                ),
            )
        )
    parallelism = value.get("parallelism")
    fabric = value.get("fabric")
    if not isinstance(parallelism, Mapping) or not isinstance(fabric, Mapping):
        raise TypeError("recipe topology execution dimensions are invalid")
    return RecipeTopology(
        name=_bounded_text(value.get("name", ""), 64),
        mode=_bounded_text(value.get("mode", ""), 64),
        node_count=max(1, _saturating_nonnegative(value.get("node_count", 1))),
        parallelism=RecipeParallelism(
            tensor=max(1, _saturating_nonnegative(parallelism.get("tensor", 1))),
            pipeline=max(1, _saturating_nonnegative(parallelism.get("pipeline", 1))),
            data=max(1, _saturating_nonnegative(parallelism.get("data", 1))),
            backend=_bounded_text(parallelism.get("backend", "unknown"), 64),
        ),
        roles=roles,
        fabric=RecipeFabric(
            connectivity=str(fabric.get("connectivity", "none")),
            minimum_bandwidth_mbps=_saturating_nonnegative(
                fabric.get("minimum_bandwidth_mbps", 0)
            ),
        ),
        start_order=[
            _bounded_text(item, 64)
            for item in value.get("start_order", [])
            if isinstance(item, str)
        ][:32],
        stop_order=[
            _bounded_text(item, 64)
            for item in value.get("stop_order", [])
            if isinstance(item, str)
        ][:32],
    )


def _canonical_recipe_summary(
    revision: CatalogDocumentRevision,
) -> LibraryRecipeSummary:
    document = revision.document if isinstance(revision.document, Mapping) else {}
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    topology = document.get("topology")
    topology = topology if isinstance(topology, Mapping) else {}
    return LibraryRecipeSummary(
        recipe_id=revision.document_id,
        slug=revision.slug,
        title=_bounded_text(metadata.get("title", revision.slug), 200),
        description=_bounded_text(metadata.get("description", ""), 4_096),
        source_kind="recipe_library",
        selected_revision=None,
        capabilities=[],
        topology_name=_bounded_text(topology.get("name", ""), 64) or None,
        installations=[],
        installation_total_count=0,
        installation_returned_count=0,
        installations_truncated=False,
        runs=[],
        run_total_count=0,
        run_returned_count=0,
        runs_truncated=False,
        reasons=[],
        recipe_capabilities=LibraryCapabilityInventory(),
    )


class LibraryProjection:
    """Read active canonical Model and Recipe revisions only."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        cursors: CursorCodec,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        inventory_fresh_seconds: int = 300,
        telemetry_live_seconds: int = 6,
        telemetry_delayed_seconds: int = 20,
        **_: object,
    ) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                inventory_fresh_seconds,
                telemetry_live_seconds,
                telemetry_delayed_seconds,
            )
        ):
            raise ValueError("Library freshness windows must be positive integers")
        if telemetry_delayed_seconds < telemetry_live_seconds:
            raise ValueError("Library telemetry freshness windows are invalid")
        self._sessions = sessions
        self._cursors = cursors
        self._clock = clock
        self._freshness = FreshnessPolicy(
            inventory_fresh_seconds=inventory_fresh_seconds,
            telemetry_live_seconds=telemetry_live_seconds,
            telemetry_delayed_seconds=telemetry_delayed_seconds,
        )

    def list(self, *, limit: int = 100, cursor: str | None = None) -> LibrarySnapshot:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("library limit is invalid")
        if cursor is not None:
            raise ValueError("canonical library pagination cursor is unsupported")
        with self._sessions() as session:
            models = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "model",
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    )
                    .limit(limit)
                )
            )
            recipes = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    )
                )
            )
        grouped: dict[tuple[str, str, str], list[LibraryRecipeSummary]] = {}
        unlinked: list[LibraryRecipeSummary] = []
        for revision in recipes:
            summary = _canonical_recipe_summary(revision)
            document = revision.document
            references = document.get("models", []) if isinstance(document, Mapping) else []
            linked = False
            if isinstance(references, list):
                for selection in references:
                    reference = selection.get("model") if isinstance(selection, Mapping) else None
                    if not isinstance(reference, Mapping):
                        continue
                    key = (
                        str(reference.get("publisher", "")),
                        str(reference.get("slug", "")),
                        str(reference.get("content_sha256", "")),
                    )
                    grouped.setdefault(key, []).append(summary)
                    linked = True
            if not linked:
                unlinked.append(summary)
        return LibrarySnapshot(
            generated_at=_utc(self._clock()),
            models=[
                LibraryModel(
                    model=ModelVersionIdentity(
                        kind="model-version",
                        publisher=revision.publisher,
                        slug=revision.slug,
                        content_sha256=revision.content_digest,
                    ),
                    recipes=grouped.get(
                        (revision.publisher, revision.slug, revision.content_digest), []
                    ),
                    model_version=None,
                )
                for revision in models
            ],
            unlinked_recipes=unlinked,
            next_cursor=None,
            freshness_policy=self._freshness,
        )

    def detail(self, recipe_id: str) -> LibraryRecipeDetail:
        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.document_id == recipe_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        if revision is None:
            raise KeyError(recipe_id)
        document = revision.document
        model = None
        if isinstance(document, Mapping) and isinstance(document.get("models"), list):
            selection = document["models"][0] if document["models"] else None
            reference = selection.get("model") if isinstance(selection, Mapping) else None
            if isinstance(reference, Mapping):
                model = ModelVersionIdentity(
                    kind="model-version",
                    publisher=str(reference["publisher"]),
                    slug=str(reference["slug"]),
                    content_sha256=str(reference["content_sha256"]),
                )
        topology = None
        if isinstance(document, Mapping) and isinstance(document.get("topology"), Mapping):
            try:
                topology = _topology(document["topology"])
            except (KeyError, TypeError, ValueError):
                topology = None
        return LibraryRecipeDetail(
            generated_at=_utc(self._clock()),
            recipe=_canonical_recipe_summary(revision),
            selected_revision=None,
            visual_recipe=None,
            topology=topology,
            operational_state=OperationalState(builds=[], mappings=[], installations=[], runs=[]),
            placement=[],
            reasons=[],
            model=model,
            model_capabilities=LibraryCapabilityInventory(),
            recipe_capabilities=LibraryCapabilityInventory(),
            model_version=None,
        )

    def recipes(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> LibraryRecipeList:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("library recipe limit is invalid")
        if cursor is not None:
            raise ValueError("canonical library recipe cursor is unsupported")
        with self._sessions() as session:
            revisions = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    )
                    .limit(limit)
                )
            )
        return LibraryRecipeList(
            generated_at=_utc(self._clock()),
            recipes=[_canonical_recipe_summary(revision) for revision in revisions],
            next_cursor=None,
            freshness_policy=self._freshness,
        )
