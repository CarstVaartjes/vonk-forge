"""Database-authoritative local recipe authoring and immutable resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .auth import CursorCodec
from .catalog_contract import (
    CatalogContractError,
    CatalogKind,
    CatalogReference,
    parse_catalog_reference,
)
from .catalog_entities import (
    CatalogConflict,
    CatalogEntityService,
    CatalogError,
    CatalogValidationError,
)
from .catalog_repository import CatalogRepository, sensitive_document_path
from .global_catalog import GlobalRecipeRevision
from .models import (
    LocalRecipe,
    LocalRecipeRevision,
    RecipeGlobalLink,
    RecipeImport,
    RecipeImportItem,
    RecipeSourceBundle,
    RecipeTestReport,
)
from .recipe_contract import (
    RecipeContractError,
    recipe_content_sha256,
    recipe_model_dependencies,
    recipe_patch_bundle,
    recipe_references,
    recipe_topology,
    validate_recipe,
)
from .schema_resources import read_runtime_schema
from .source_bundles import SourceBundleError, SourceBundleStore

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MUTABLE_REVISIONS = frozenset(
    {"main", "master", "latest", "head", "main-latest", "master-latest"}
)
_REQUIRED_TEST_CHECKS = frozenset(
    {"container.started", "endpoint.healthy", "inference.completed"}
)


@dataclass(frozen=True, slots=True)
class RecipeDraftInput:
    slug: str
    document: Mapping[str, object]
    source_kind: str = "local"


@dataclass(frozen=True, slots=True)
class RecipeRevisionView:
    id: str
    recipe_id: str
    slug: str
    title: str
    description: str
    source_kind: str
    revision_number: int
    lifecycle: str
    schema_version: int
    document: dict[str, object]
    content_sha256: str | None
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecipeSummary:
    recipe_id: str
    slug: str
    title: str
    source_kind: str
    revision_number: int
    lifecycle: str
    content_sha256: str | None
    execution_harness: str
    runtime_distribution: str
    source_bundle_sha256: str
    artifact_count: int
    expected_download_bytes: int
    topology_name: str
    topology_mode: str
    node_count: int
    maximum_installed_bytes_per_node: int
    maximum_runtime_memory_bytes_per_node: int


@dataclass(frozen=True, slots=True)
class SourceBundleView:
    sha256: str
    archive_bytes: int
    total_bytes: int
    file_count: int
    files: tuple[str, ...]


class CatalogService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        cursors: CursorCodec,
        repository: CatalogRepository | None = None,
        source_bundles: SourceBundleStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._repository = repository or CatalogRepository()
        self._source_bundles = source_bundles
        self._cursors = cursors
        self.entities = CatalogEntityService(sessions, clock=clock, cursors=cursors)

    def store_source_bundle(
        self, expected_sha256: str, payload: BinaryIO, actor: str
    ) -> SourceBundleView:
        del actor  # Attribution is captured by the API audit record.
        if self._source_bundles is None:
            raise CatalogError(
                "bundle.storage_unavailable", "source bundle storage is unavailable"
            )
        try:
            stored = self._source_bundles.put(expected_sha256, payload)
        except SourceBundleError as error:
            raise CatalogValidationError(error.code, error.detail) from error
        manifest = stored.manifest
        row = RecipeSourceBundle(
            sha256=manifest.sha256,
            media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
            archive_bytes=stored.archive_bytes,
            total_bytes=manifest.total_bytes,
            file_count=len(manifest.files),
            storage_key=f"{manifest.sha256[:2]}/{manifest.sha256}.tar",
            manifest={
                "schema_version": 1,
                "files": [asdict(item) for item in manifest.files],
                "total_bytes": manifest.total_bytes,
                "sha256": manifest.sha256,
            },
            verified_at=self._clock(),
        )
        try:
            with self._sessions.begin() as session:
                existing = session.get(RecipeSourceBundle, manifest.sha256)
                if existing is None:
                    session.add(row)
                else:
                    row = existing
        except IntegrityError as error:
            raise CatalogConflict(
                "bundle.storage_conflict", "source bundle metadata conflicts"
            ) from error
        return SourceBundleView(
            sha256=row.sha256,
            archive_bytes=row.archive_bytes,
            total_bytes=row.total_bytes,
            file_count=row.file_count,
            files=tuple(item.path for item in manifest.files),
        )

    def read_source_bundle(self, sha256: str) -> bytes:
        """Return a verified local source archive for publication or inspection."""

        if self._source_bundles is None:
            raise CatalogError(
                "bundle.storage_unavailable", "source bundle storage is unavailable"
            )
        with self._sessions() as session:
            row = session.get(RecipeSourceBundle, sha256)
            if row is None:
                raise KeyError(sha256)
            expected = (row.archive_bytes, row.total_bytes, row.file_count)
        try:
            stored = self._source_bundles.get(sha256)
        except SourceBundleError as error:
            raise CatalogValidationError(error.code, error.detail) from error
        observed = (
            len(stored.archive),
            stored.manifest.total_bytes,
            len(stored.manifest.files),
        )
        if observed != expected:
            raise CatalogValidationError(
                "bundle.metadata_mismatch",
                "source bundle storage does not match its database metadata",
            )
        return stored.archive

    def list_recipes(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[RecipeSummary], str | None]:
        if not 1 <= limit <= 100:
            raise CatalogValidationError("catalog.limit", "catalog limit is invalid")
        latest_numbers = (
            select(
                LocalRecipeRevision.recipe_id,
                func.max(LocalRecipeRevision.revision_number).label("revision_number"),
            )
            .group_by(LocalRecipeRevision.recipe_id)
            .subquery()
        )
        with self._sessions() as session:
            statement = (
                select(LocalRecipe, LocalRecipeRevision)
                .join(latest_numbers, latest_numbers.c.recipe_id == LocalRecipe.id)
                .join(
                    LocalRecipeRevision,
                    and_(
                        LocalRecipeRevision.recipe_id == LocalRecipe.id,
                        LocalRecipeRevision.revision_number
                        == latest_numbers.c.revision_number,
                    ),
                )
                .order_by(LocalRecipe.updated_at.desc(), LocalRecipe.id.desc())
            )
            if cursor is not None:
                boundary = self._repository.recipe(session, cursor)
                if boundary is None:
                    raise CatalogValidationError(
                        "catalog.cursor", "catalog cursor is invalid"
                    )
                statement = statement.where(
                    or_(
                        LocalRecipe.updated_at < boundary.updated_at,
                        and_(
                            LocalRecipe.updated_at == boundary.updated_at,
                            LocalRecipe.id < boundary.id,
                        ),
                    )
                )
            rows = session.execute(statement.limit(limit + 1)).all()
        page = rows[:limit]
        summaries = [_summary(recipe, revision) for recipe, revision in page]
        next_cursor = page[-1][0].id if len(rows) > limit else None
        return summaries, next_cursor

    def get_recipe(self, recipe_id: str) -> RecipeRevisionView:
        with self._sessions() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe_id)
            if revision is None:
                raise KeyError(recipe_id)
            return _view(recipe, revision)

    def create_recipe(self, actor: str, draft: RecipeDraftInput) -> RecipeRevisionView:
        document = self._validated_document(draft.document, slug=draft.slug)
        if draft.source_kind not in {
            "local",
            "workload_run",
            "global",
            "recipe_library",
        }:
            raise CatalogValidationError("catalog.source_kind", "unknown source kind")
        metadata = _mapping(document["metadata"])
        now = self._clock()
        recipe = LocalRecipe(
            slug=draft.slug,
            title=str(metadata["title"]),
            description=str(metadata["description"]),
            source_kind=draft.source_kind,
            created_by=_actor(actor),
            created_at=now,
            updated_at=now,
        )
        revision = LocalRecipeRevision(
            recipe_id="",
            revision_number=1,
            lifecycle="draft",
            schema_version=1,
            document=document,
            content_sha256=None,
            created_by=_actor(actor),
            created_at=now,
        )
        # The models intentionally have no relationship cascade; flush the parent first.
        try:
            with self._sessions.begin() as session:
                session.add(recipe)
                session.flush()
                revision.recipe_id = recipe.id
                session.add(revision)
                session.flush()
                view = _view(recipe, revision)
        except IntegrityError as error:
            raise CatalogConflict(
                "catalog.slug_exists", "recipe slug already exists"
            ) from error
        return view

    def update_draft(
        self,
        recipe_id: str,
        expected_revision: int,
        document: Mapping[str, object],
        actor: str,
    ) -> RecipeRevisionView:
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id, for_update=True)
            clean = self._validated_document(document, slug=recipe.slug)
            latest = self._repository.latest_revision(session, recipe_id)
            if latest is None or latest.revision_number != expected_revision:
                raise CatalogConflict(
                    "catalog.stale_revision", "recipe revision changed"
                )
            if latest.lifecycle == "deprecated":
                raise CatalogConflict(
                    "catalog.deprecated", "deprecated recipe cannot be revised"
                )
            metadata = _mapping(clean["metadata"])
            now = self._clock()
            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=self._repository.next_revision_number(
                    session, recipe.id
                ),
                lifecycle="draft",
                schema_version=1,
                document=clean,
                content_sha256=None,
                created_by=_actor(actor),
                created_at=now,
            )
            recipe.title = str(metadata["title"])
            recipe.description = str(metadata["description"])
            recipe.updated_at = now
            session.add(revision)
            session.flush()
            return _view(recipe, revision)

    def resolve(
        self, recipe_id: str, expected_revision: int, actor: str
    ) -> RecipeRevisionView:
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id, for_update=True)
            latest = self._repository.latest_revision(session, recipe_id)
            if latest is None:
                raise KeyError(recipe_id)
            if latest.lifecycle == "resolved":
                if expected_revision in {
                    latest.revision_number,
                    latest.revision_number - 1,
                }:
                    return _view(recipe, latest)
                raise CatalogConflict(
                    "catalog.stale_revision", "recipe revision changed"
                )
            if latest.revision_number != expected_revision:
                raise CatalogConflict(
                    "catalog.stale_revision", "recipe revision changed"
                )
            if recipe.source_kind == "workload_run":
                unresolved = session.scalar(
                    select(RecipeImportItem.id)
                    .join(RecipeImport, RecipeImport.id == RecipeImportItem.import_id)
                    .where(
                        RecipeImport.recipe_id == recipe_id,
                        RecipeImportItem.disposition.in_(
                            (
                                "resolution_required",
                                "overlay_required",
                                "unsupported_blocking",
                            )
                        ),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    raise CatalogConflict(
                        "catalog.import_unresolved",
                        "import report must be resolved before this recipe can run",
                    )
            clean = self._validated_document(latest.document, slug=recipe.slug)
            digest = self._resolve_recipe_revision(session, clean, actor=actor)
            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=self._repository.next_revision_number(
                    session, recipe.id
                ),
                lifecycle="resolved",
                schema_version=1,
                document=clean,
                content_sha256=digest,
                created_by=_actor(actor),
                created_at=self._clock(),
            )
            session.add(revision)
            session.flush()
            return _view(recipe, revision)

    def resolve_recipe_revision(
        self,
        document: Mapping[str, object],
        *,
        actor: str,
    ) -> str:
        """Validate every exact entity binding before returning the recipe digest."""

        clean = copy.deepcopy(dict(document))
        try:
            validate_recipe(clean)
        except RecipeContractError as error:
            raise CatalogValidationError(
                error.code, f"{error.path}: {error.detail}"
            ) from error
        with self._sessions() as session:
            return self._resolve_recipe_revision(session, clean, actor=actor)

    def _resolve_recipe_revision(
        self,
        session: Session,
        document: Mapping[str, object],
        *,
        actor: str,
    ) -> str:
        _actor(actor)
        entity_service = CatalogEntityService(
            session, clock=self._clock, cursors=self._cursors
        )
        references = recipe_references(document)
        model_version_ref, harness_ref, distribution_ref = references[:3]
        patch_ref = recipe_patch_bundle(document)
        model_version = entity_service.lookup_exact(
            *model_version_ref.portable_identity
        )
        model_ref = _catalog_reference(
            model_version.document, "model", CatalogKind.MODEL
        )
        model = entity_service.lookup_exact(*model_ref.portable_identity)
        group_ref = _catalog_reference(
            model.document, "model_group", CatalogKind.MODEL_GROUP
        )
        entity_service.lookup_exact(*group_ref.portable_identity)
        entity_service.lookup_exact(*harness_ref.portable_identity)
        distribution = entity_service.lookup_exact(*distribution_ref.portable_identity)
        implemented_harness = _catalog_reference(
            distribution.document,
            "implements_harness",
            CatalogKind.EXECUTION_HARNESS,
        )
        if implemented_harness != harness_ref:
            raise CatalogConflict(
                "catalog.harness_distribution_mismatch",
                "runtime distribution does not implement the exact harness",
            )
        if patch_ref is not None:
            patch = entity_service.lookup_exact(*patch_ref.portable_identity)
            applies_to = _catalog_reference(
                patch.document, "applies_to", CatalogKind.RUNTIME_DISTRIBUTION
            )
            if applies_to != distribution_ref:
                raise CatalogConflict(
                    "catalog.patch_distribution_mismatch",
                "patch bundle does not declare the recipe's exact distribution",
            )
        for dependency_ref in recipe_model_dependencies(document):
            dependency = entity_service.lookup_exact(*dependency_ref.portable_identity)
            model_ref = _catalog_reference(
                dependency.document, "model", CatalogKind.MODEL
            )
            model = entity_service.lookup_exact(*model_ref.portable_identity)
            group_ref = _catalog_reference(
                model.document, "model_group", CatalogKind.MODEL_GROUP
            )
            entity_service.lookup_exact(*group_ref.portable_identity)
        return recipe_content_sha256(document)

    def fork(
        self,
        recipe_id: str,
        revision_number: int,
        slug: str,
        actor: str,
    ) -> RecipeRevisionView:
        with self._sessions() as session:
            source_recipe = self._require_recipe(session, recipe_id)
            source = self._repository.revision(session, recipe_id, revision_number)
            if source is None:
                raise KeyError((recipe_id, revision_number))
            document = copy.deepcopy(source.document)
        identity = _mapping(document["identity"])
        identity["slug"] = slug
        provenance = _mapping(document["provenance"])
        provenance["source_kind"] = "fork"
        provenance["source_reference"] = f"local:{source_recipe.slug}:{revision_number}"
        attribution = list(provenance.get("attribution", []))
        identity_digest = source.content_sha256 or recipe_content_sha256(
            source.document
        )
        attribution.append(f"forked from {source_recipe.slug}@sha256:{identity_digest}")
        provenance["attribution"] = attribution
        return self.create_recipe(
            actor, RecipeDraftInput(slug=slug, document=document, source_kind="local")
        )

    def import_global(
        self, actor: str, remote: GlobalRecipeRevision
    ) -> RecipeRevisionView:
        """Materialize one verified global revision in authoritative local rows."""

        actor = _actor(actor)
        clean = self._validated_document(remote.document, slug=remote.slug)
        if recipe_content_sha256(clean) != remote.content_sha256:
            raise CatalogValidationError(
                "global.hash_mismatch", "global recipe content hash is invalid"
            )
        identity = _mapping(clean["identity"])
        if identity != {"publisher": remote.publisher, "slug": remote.slug}:
            raise CatalogValidationError(
                "global.identity_mismatch", "global recipe identity is inconsistent"
            )
        with self._sessions.begin() as session:
            self._resolve_recipe_revision(session, clean, actor=actor)
            imported = session.scalar(
                select(RecipeImport).where(
                    RecipeImport.source_kind == "global",
                    RecipeImport.source_sha256 == remote.content_sha256,
                )
            )
            if imported is not None:
                recipe = self._require_recipe(session, imported.recipe_id)
                revision = session.scalar(
                    select(LocalRecipeRevision).where(
                        LocalRecipeRevision.recipe_id == recipe.id,
                        LocalRecipeRevision.content_sha256 == remote.content_sha256,
                    )
                )
                if revision is None:
                    raise CatalogConflict(
                        "global.history_inconsistent",
                        "local import history is inconsistent",
                    )
                return _view(recipe, revision)

            link = session.scalar(
                select(RecipeGlobalLink).where(
                    RecipeGlobalLink.global_publisher == remote.publisher,
                    RecipeGlobalLink.global_slug == remote.slug,
                )
            )
            metadata = _mapping(clean["metadata"])
            now = self._clock()
            if link is None:
                if self._repository.recipe_by_slug(session, remote.slug) is not None:
                    raise CatalogConflict(
                        "global.slug_conflict",
                        "a different local recipe already uses this slug; fork or rename it",
                    )
                recipe = LocalRecipe(
                    slug=remote.slug,
                    title=str(metadata["title"]),
                    description=str(metadata["description"]),
                    source_kind="global",
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(recipe)
                session.flush()
                local_number = 1
            else:
                recipe = self._require_recipe(session, link.recipe_id, for_update=True)
                if remote.revision_number <= link.global_revision:
                    raise CatalogConflict(
                        "global.revision_stale",
                        "requested global revision is older than the local imported revision",
                    )
                local_number = self._repository.next_revision_number(session, recipe.id)
                recipe.title = str(metadata["title"])
                recipe.description = str(metadata["description"])
                recipe.updated_at = now

            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=local_number,
                lifecycle="resolved",
                schema_version=1,
                document=clean,
                content_sha256=remote.content_sha256,
                created_by=actor,
                created_at=now,
            )
            session.add(revision)
            session.flush()
            session.add(
                RecipeImport(
                    recipe_id=recipe.id,
                    source_kind="global",
                    source_reference=remote.uri,
                    source_sha256=remote.content_sha256,
                    redacted_source={
                        "publisher": remote.publisher,
                        "slug": remote.slug,
                        "recipe_id": remote.recipe_id,
                        "revision_number": remote.revision_number,
                        "revision_id": remote.revision_id,
                        "published_at": remote.published_at,
                    },
                    created_by=actor,
                    created_at=now,
                )
            )
            if link is None:
                link = RecipeGlobalLink(
                    recipe_id=recipe.id,
                    global_recipe_id=remote.recipe_id,
                    global_publisher=remote.publisher,
                    global_slug=remote.slug,
                    global_revision=remote.revision_number,
                    global_content_sha256=remote.content_sha256,
                    sync_state="current",
                    synced_at=now,
                )
                session.add(link)
            else:
                link.global_recipe_id = remote.recipe_id
                link.global_revision = remote.revision_number
                link.global_content_sha256 = remote.content_sha256
                link.sync_state = "current"
                link.synced_at = now
            session.flush()
            return _view(recipe, revision)

    def import_recipe_library(
        self,
        actor: str,
        *,
        library_commit: str,
        source_path: str,
        document: Mapping[str, object],
        expected_content_sha256: str,
    ) -> RecipeRevisionView:
        """Import one exact recipe from the public Git recipe library."""

        actor = _actor(actor)
        raw_identity = _mapping(document.get("identity"))
        clean = self._validated_document(
            document, slug=str(raw_identity.get("slug", ""))
        )
        actual = recipe_content_sha256(clean)
        if actual != expected_content_sha256:
            raise CatalogValidationError(
                "recipe_library.hash_mismatch",
                "recipe content does not match the supplied digest",
            )
        identity = _mapping(clean["identity"])
        publisher = str(identity["publisher"])
        slug = str(identity["slug"])
        source_reference = (
            "https://github.com/CarstVaartjes/vonk-forge-recipes@"
            f"{library_commit}:{source_path}"
        )
        with self._sessions.begin() as session:
            imported = session.scalar(
                select(RecipeImport).where(
                    RecipeImport.source_kind == "recipe_library",
                    RecipeImport.source_sha256 == actual,
                )
            )
            if imported is not None:
                recipe = self._require_recipe(session, imported.recipe_id)
                revision = session.scalar(
                    select(LocalRecipeRevision).where(
                        LocalRecipeRevision.recipe_id == recipe.id,
                        LocalRecipeRevision.content_sha256 == actual,
                        LocalRecipeRevision.lifecycle == "resolved",
                    )
                )
                if revision is None:
                    raise CatalogConflict(
                        "recipe_library.history_inconsistent",
                        "recipe library import history is inconsistent",
                    )
                return _view(recipe, revision)

            recipe = self._repository.recipe_by_slug(session, slug)
            now = self._clock()
            if recipe is None:
                metadata = _mapping(clean["metadata"])
                recipe = LocalRecipe(
                    slug=slug,
                    title=str(metadata["title"]),
                    description=str(metadata["description"]),
                    source_kind="recipe_library",
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(recipe)
                session.flush()
            elif recipe.source_kind != "recipe_library":
                raise CatalogConflict(
                    "recipe_library.slug_conflict",
                    "a non-library recipe already uses this slug",
                )

            revision = session.scalar(
                select(LocalRecipeRevision).where(
                    LocalRecipeRevision.recipe_id == recipe.id,
                    LocalRecipeRevision.content_sha256 == actual,
                    LocalRecipeRevision.lifecycle == "resolved",
                )
            )
            if revision is None:
                self._resolve_recipe_revision(session, clean, actor=actor)
                metadata = _mapping(clean["metadata"])
                revision = LocalRecipeRevision(
                    recipe_id=recipe.id,
                    revision_number=self._repository.next_revision_number(
                        session, recipe.id
                    ),
                    lifecycle="resolved",
                    schema_version=1,
                    document=clean,
                    content_sha256=actual,
                    created_by=actor,
                    created_at=now,
                )
                recipe.title = str(metadata["title"])
                recipe.description = str(metadata["description"])
                recipe.updated_at = now
                session.add(revision)
                session.flush()

            session.add(
                RecipeImport(
                    recipe_id=recipe.id,
                    source_kind="recipe_library",
                    source_reference=source_reference,
                    source_sha256=actual,
                    redacted_source={
                        "repository": "CarstVaartjes/vonk-forge-recipes",
                        "commit": library_commit,
                        "path": source_path,
                        "publisher": publisher,
                        "slug": slug,
                    },
                    created_by=actor,
                    created_at=now,
                )
            )
            session.flush()
            return _view(recipe, revision)

    def attach_test_report(
        self, recipe_id: str, report: Mapping[str, object], actor: str
    ) -> dict[str, object]:
        """Validate publisher evidence without claiming Vonk certification."""

        actor = _actor(actor)
        sensitive = sensitive_document_path(report)
        if sensitive is not None:
            raise CatalogValidationError(
                "catalog.sensitive_field",
                f"sensitive field is forbidden at {sensitive}",
            )
        clean: dict[str, object] = copy.deepcopy(dict(report))
        errors = sorted(
            _test_report_validator().iter_errors(clean),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            path = ".".join(map(str, errors[0].absolute_path)) or "$"
            raise CatalogValidationError(
                "catalog.test_report_invalid", f"test report is invalid at {path}"
            )
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe.id)
            if (
                revision is None
                or revision.lifecycle != "resolved"
                or revision.content_sha256 is None
            ):
                raise CatalogConflict(
                    "catalog.recipe_unresolved",
                    "resolve the recipe before attaching a test report",
                )
            build = _mapping(revision.document["build"])
            context = _mapping(build["context"])
            try:
                topology = recipe_topology(revision.document)
            except RecipeContractError as error:
                raise CatalogValidationError(
                    "catalog.test_report_topology_mismatch",
                    "test report topology is not declared by this recipe",
                ) from error
            by_name = {
                str(item.get("name")): item.get("passed")
                for item in clean["checks"]
                if isinstance(item, Mapping)
            }
            if clean.get("recipe_sha256") != revision.content_sha256:
                raise CatalogValidationError(
                    "catalog.test_report_recipe_mismatch",
                    "test report does not match this recipe revision",
                )
            if clean.get("source_bundle_sha256") != context["sha256"]:
                raise CatalogValidationError(
                    "catalog.test_report_source_bundle_mismatch",
                    "test report does not match this recipe source bundle",
                )
            if (
                clean.get("topology_name") != topology["name"]
                or clean.get("node_count") != topology["node_count"]
            ):
                raise CatalogValidationError(
                    "catalog.test_report_topology_mismatch",
                    "test report does not match the recipe topology",
                )
            if any(by_name.get(name) is not True for name in _REQUIRED_TEST_CHECKS):
                raise CatalogValidationError(
                    "catalog.test_report_failed",
                    "test report must show all required lifecycle and inference checks passed",
                )
            try:
                started = datetime.fromisoformat(str(clean["started_at"])).astimezone(
                    UTC
                )
                finished = datetime.fromisoformat(str(clean["finished_at"])).astimezone(
                    UTC
                )
            except ValueError as error:
                raise CatalogValidationError(
                    "catalog.test_report_timestamps",
                    "test report timestamps are invalid",
                ) from error
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            else:
                now = now.astimezone(UTC)
            if not (
                started <= finished
                and finished - started <= timedelta(hours=24)
                and now - timedelta(days=90) <= finished <= now + timedelta(minutes=5)
            ):
                raise CatalogValidationError(
                    "catalog.test_report_timestamps",
                    "test report timestamps must be ordered, recent, and within 24 hours",
                )
            encoded = json.dumps(
                clean,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            existing = session.scalar(
                select(RecipeTestReport).where(
                    RecipeTestReport.recipe_revision_id == revision.id,
                    RecipeTestReport.report_sha256 == digest,
                )
            )
            if existing is None:
                session.add(
                    RecipeTestReport(
                        recipe_revision_id=revision.id,
                        report_sha256=digest,
                        report=clean,
                        created_by=actor,
                        created_at=self._clock(),
                    )
                )
            return copy.deepcopy(clean)

    def publication_export(
        self, recipe_id: str, target_publisher: str
    ) -> dict[str, object]:
        if not _SLUG.fullmatch(target_publisher):
            raise CatalogValidationError(
                "catalog.publisher", "target publisher namespace is invalid"
            )
        with self._sessions() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe.id)
            if (
                revision is None
                or revision.lifecycle != "resolved"
                or revision.content_sha256 is None
            ):
                raise CatalogConflict(
                    "catalog.recipe_unresolved",
                    "resolve the recipe before exporting it",
                )
            evidence = session.scalar(
                select(RecipeTestReport)
                .where(RecipeTestReport.recipe_revision_id == revision.id)
                .order_by(
                    RecipeTestReport.created_at.desc(), RecipeTestReport.id.desc()
                )
                .limit(1)
            )
            if evidence is None:
                raise CatalogConflict(
                    "catalog.test_report_required",
                    "attach a passing local test report before publication export",
                )
            document = copy.deepcopy(revision.document)
            report = copy.deepcopy(evidence.report)
        identity = _mapping(document["identity"])
        identity["publisher"] = target_publisher
        validate_recipe(document)
        report["recipe_sha256"] = recipe_content_sha256(document)
        return {"recipe": document, "test_report": report}

    def _require_recipe(
        self, session: Session, recipe_id: str, *, for_update: bool = False
    ) -> LocalRecipe:
        recipe = self._repository.recipe(session, recipe_id, for_update=for_update)
        if recipe is None:
            raise KeyError(recipe_id)
        return recipe

    def _validated_document(
        self, document: Mapping[str, object], *, slug: str
    ) -> dict[str, object]:
        if not _SLUG.fullmatch(slug):
            raise CatalogValidationError("catalog.slug", "recipe slug is invalid")
        sensitive = sensitive_document_path(document)
        if sensitive is not None:
            raise CatalogValidationError(
                "catalog.sensitive_field",
                f"sensitive field is forbidden at {sensitive}",
            )
        clean: dict[str, object] = copy.deepcopy(dict(document))
        artifacts = clean.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                revision = str(artifact.get("revision", "")).lower()
                if revision in _MUTABLE_REVISIONS or revision.endswith("-latest"):
                    raise CatalogValidationError(
                        "catalog.mutable_artifact",
                        "artifact revision must be immutable",
                    )
        try:
            validate_recipe(clean)
        except RecipeContractError as error:
            raise CatalogValidationError(
                error.code, f"{error.path}: {error.detail}"
            ) from error
        identity = _mapping(clean["identity"])
        if identity.get("slug") != slug:
            raise CatalogValidationError(
                "catalog.slug_mismatch", "recipe identity slug does not match"
            )
        return clean


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogValidationError("catalog.document", "recipe object is invalid")
    return value


@lru_cache(maxsize=1)
def _test_report_validator() -> Draft202012Validator:
    schema = json.loads(read_runtime_schema("test-report-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _actor(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise CatalogValidationError("catalog.actor", "catalog actor is invalid")
    return normalized


def _view(recipe: LocalRecipe, revision: LocalRecipeRevision) -> RecipeRevisionView:
    return RecipeRevisionView(
        id=revision.id,
        recipe_id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        description=recipe.description,
        source_kind=recipe.source_kind,
        revision_number=revision.revision_number,
        lifecycle=revision.lifecycle,
        schema_version=revision.schema_version,
        document=copy.deepcopy(revision.document),
        content_sha256=revision.content_sha256,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _summary(recipe: LocalRecipe, revision: LocalRecipeRevision) -> RecipeSummary:
    metrics = _document_summary(revision.document)
    return RecipeSummary(
        recipe_id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        source_kind=recipe.source_kind,
        revision_number=revision.revision_number,
        lifecycle=revision.lifecycle,
        content_sha256=revision.content_sha256,
        **metrics,
    )


def _document_summary(document: Mapping[str, object]) -> dict[str, Any]:
    runtime = _mapping(document["runtime"])
    execution = _mapping(document["execution"])
    harness = _mapping(execution["harness"])
    distribution = _mapping(runtime["distribution"])
    build = _mapping(document["build"])
    context = _mapping(build["context"])
    artifacts = document["artifacts"]
    topology = _mapping(document["topology"])
    assert isinstance(artifacts, list)
    installed: list[int] = []
    runtime_memory: list[int] = []
    roles = topology["roles"]
    assert isinstance(roles, list)
    for role_value in roles:
        role = _mapping(role_value)
        resources = _mapping(role["resources"])
        disk = _mapping(resources["disk"])
        memory = _mapping(resources["memory"])
        installed.append(
            int(disk["image_bytes"])
            + int(disk["artifact_bytes"])
            + int(disk["cache_bytes"])
            + int(disk["rollback_bytes"])
        )
        runtime_memory.append(
            max(
                int(memory["startup_peak_bytes"]),
                int(memory["steady_state_bytes"]) + int(memory["runtime_growth_bytes"]),
            )
            + int(memory["system_reserve_bytes"])
        )
    return {
        "execution_harness": str(harness["slug"]),
        "runtime_distribution": str(distribution["slug"]),
        "source_bundle_sha256": str(context["sha256"]),
        "artifact_count": len(artifacts),
        "expected_download_bytes": sum(
            int(_mapping(artifact)["download_bytes"]) for artifact in artifacts
        ),
        "topology_name": str(topology["name"]),
        "topology_mode": str(topology["mode"]),
        "node_count": int(topology["node_count"]),
        "maximum_installed_bytes_per_node": max(installed),
        "maximum_runtime_memory_bytes_per_node": max(runtime_memory),
    }


def _catalog_reference(
    document: Mapping[str, object], field: str, expected_kind: CatalogKind
) -> CatalogReference:
    value = document.get(field)
    if not isinstance(value, Mapping):
        raise CatalogValidationError(
            "catalog.reference", f"catalog {field} reference is invalid"
        )
    try:
        return parse_catalog_reference(value, expected_kind=expected_kind)
    except CatalogContractError as error:
        raise CatalogValidationError(error.code, error.detail) from error
