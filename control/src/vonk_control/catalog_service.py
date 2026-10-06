"""Canonical Model and Recipe catalog persistence."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import IO

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    CatalogCode,
    SourceBundleCode,
    canonical_message,
)
from vonk_forge_contracts import (
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)

from .auth import CursorCodec
from .catalog_entities import (
    CatalogConflict,
    CatalogDocumentRevision,
    CatalogEntityService,
    CatalogError,
    CatalogValidationError,
)
from .catalog_queries import active_head_revision
from .catalog_revision_contract import (
    PrebuiltImage,
    read_catalog_document,
    read_catalog_projection,
    write_catalog_projection,
)
from .models import (
    CatalogDocument,
    CatalogDocumentHead,
    RecipeSourceBundle,
)
from .recipe_runtime_specs import RecipeRuntimeSpecError, recipe_topology
from .source_bundles import (
    SourceBundleError,
    SourceBundleStoreProtocol,
    parse_source_bundle_manifest,
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")


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
class RecipeCatalogLocalRevision:
    recipe_id: str
    source_kind: str
    publisher: str
    slug: str
    revision_number: int
    content_sha256: str | None
    release_version: str | None
    # Spark count of the active revision; None when it cannot be read.
    node_count: int | None = None
    # The package the active revision's source bundle was imported from; None
    # when an older import recorded none or the projection cannot be read.
    package_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SourceBundleView:
    sha256: str
    archive_bytes: int
    total_bytes: int
    file_count: int
    files: tuple[str, ...]


class CatalogService:
    """Read and activate only canonical schema-2 Model and Recipe documents."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        cursors: CursorCodec,
        repository: object | None = None,
        source_bundles: SourceBundleStoreProtocol | None = None,
    ) -> None:
        del repository
        self._sessions = sessions
        self._clock = clock
        self._source_bundles = source_bundles
        self._cursors = cursors
        self.entities = CatalogEntityService(sessions, clock=clock, cursors=cursors)

    def store_source_bundle(
        self, expected_sha256: str, payload: IO[bytes], actor: str
    ) -> SourceBundleView:
        del actor
        if self._source_bundles is None:
            raise CatalogError(
                SourceBundleCode.STORAGE_UNAVAILABLE,
                "source bundle storage is unavailable",
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
            manifest=json.loads(canonical_message(manifest)),
            verified_at=self._clock(),
        )
        try:
            with self._sessions.begin() as session:
                existing = session.get(RecipeSourceBundle, manifest.sha256)
                if existing is None:
                    session.add(row)
                else:
                    if parse_source_bundle_manifest(existing.manifest) != manifest:
                        raise CatalogValidationError(
                            SourceBundleCode.METADATA_MISMATCH,
                            "stored source manifest differs from verified bundle",
                        )
                    row = existing
        except IntegrityError as error:
            raise CatalogConflict(
                SourceBundleCode.STORAGE_CONFLICT, "source bundle metadata conflicts"
            ) from error
        except SourceBundleError as error:
            raise CatalogValidationError(error.code, error.detail) from error
        return SourceBundleView(
            sha256=row.sha256,
            archive_bytes=row.archive_bytes,
            total_bytes=row.total_bytes,
            file_count=row.file_count,
            files=tuple(item.path for item in manifest.files),
        )

    def get_recipe(self, recipe_id: str) -> RecipeRevisionView:
        with self._sessions() as session:
            revision = _get_active_recipe(session, recipe_id)
        if revision is None:
            raise KeyError(recipe_id)
        return _view(revision)

    def recipe_catalog_local_revisions(
        self, identities: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], RecipeCatalogLocalRevision]:
        requested = sorted(set(identities))
        if any(
            not isinstance(publisher, str)
            or not isinstance(slug, str)
            or not _SLUG.fullmatch(publisher)
            or not _SLUG.fullmatch(slug)
            for publisher, slug in requested
        ):
            raise CatalogValidationError(
                CatalogCode.IDENTITIES, "catalog recipe identities are invalid"
            )
        if not requested:
            return {}
        result: dict[tuple[str, str], RecipeCatalogLocalRevision] = {}
        requested_set = set(requested)
        # Keep each statement small as the publication grows, while matching
        # the full canonical identity rather than crossing publisher/slug IN
        # lists or collapsing entries that happen to share a slug.
        for offset in range(0, len(requested), 128):
            batch = requested[offset : offset + 128]
            predicates = [
                and_(
                    CatalogDocumentRevision.publisher == publisher,
                    CatalogDocumentRevision.slug == slug,
                )
                for publisher, slug in batch
            ]
            with self._sessions() as session:
                rows = list(
                    session.scalars(
                        select(CatalogDocumentRevision).where(
                            CatalogDocumentRevision.kind == "recipe",
                            CatalogDocumentRevision.state == "active",
                            active_head_revision(),
                            or_(*predicates),
                        )
                    )
                )
            for row in rows:
                identity = (row.publisher, row.slug)
                if identity in requested_set:
                    result[identity] = RecipeCatalogLocalRevision(
                        recipe_id=row.document_id,
                        source_kind="recipe_library",
                        publisher=row.publisher,
                        slug=row.slug,
                        revision_number=row.revision_number,
                        content_sha256=row.content_digest,
                        release_version=_release_version(row.document),
                        node_count=_node_count(row.document),
                        package_sha256=_projected_package_sha256(row),
                    )
        return result

    def retract_recipes_absent_from(
        self, published: Sequence[tuple[str, str]]
    ) -> list[RecipeCatalogLocalRevision]:
        """Stop offering every recipe the published library no longer lists.

        The recipe's head is cleared; its immutable revisions stay, so
        installations and runs that reference them keep working until they are
        replaced or retention removes them.  A recipe that is published again
        becomes current through the normal import.
        """
        keep = set(published)
        retracted: list[RecipeCatalogLocalRevision] = []
        with self._sessions.begin() as session:
            heads = session.scalars(
                select(CatalogDocumentHead)
                .where(
                    CatalogDocumentHead.kind == "recipe",
                    CatalogDocumentHead.active_revision_id.is_not(None),
                )
                .with_for_update()
            ).all()
            for head in heads:
                identity = (head.publisher, head.slug)
                if identity in keep:
                    continue
                revision = session.get(CatalogDocumentRevision, head.active_revision_id)
                head.active_revision_id = None
                head.generation += 1
                root = session.scalar(
                    select(CatalogDocument).where(
                        CatalogDocument.kind == "recipe",
                        CatalogDocument.publisher == head.publisher,
                        CatalogDocument.slug == head.slug,
                    )
                )
                if root is not None:
                    root.updated_at = self._clock()
                if revision is not None:
                    retracted.append(
                        RecipeCatalogLocalRevision(
                            recipe_id=revision.document_id,
                            source_kind="recipe_library",
                            publisher=revision.publisher,
                            slug=revision.slug,
                            revision_number=revision.revision_number,
                            content_sha256=revision.content_digest,
                            release_version=_release_version(revision.document),
                        )
                    )
        return retracted

    def import_catalog_models(
        self, actor: str, documents: Sequence[Mapping[str, object]]
    ) -> int:
        actor = _actor(actor)
        try:
            for value in documents:
                read_model(value)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError(
                CatalogCode.MODEL_DOCUMENT_INVALID,
                "catalog index model documents are invalid",
            ) from error
        with self._sessions.begin() as session:
            for value in documents:
                self._upsert_canonical_document(session, value, actor=actor)
        return len(documents)

    def refresh_build_policy(self) -> None:
        CatalogEntityService(self._sessions, clock=self._clock).refresh_build_policy()

    def record_prebuilt_images(
        self, images: Mapping[tuple[str, str, str], PrebuiltImage | None]
    ) -> None:
        CatalogEntityService(self._sessions, clock=self._clock).record_prebuilt_images(
            images
        )

    def import_recipe_library(
        self,
        actor: str,
        *,
        library_commit: str,
        source_path: str,
        document: Mapping[str, object],
        expected_content_sha256: str,
        dependency_documents: Sequence[Mapping[str, object]] = (),
        release_version: str | None = None,
        release_released_at: str | None = None,
        package_handle: object | None = None,
        package_sha256: str | None = None,
        source_bundle_sha256: str | None = None,
    ) -> RecipeRevisionView:
        actor = _actor(actor)
        try:
            read_recipe(document)
            for value in dependency_documents:
                read_model(value)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError(
                CatalogCode.DOCUMENT_INVALID_,
                "recipe library package must contain a canonical recipe and model snapshots",
            ) from error
        if document_sha256(document) != expected_content_sha256:
            raise CatalogValidationError(
                CatalogCode.HASH_MISMATCH,
                "recipe content does not match the supplied digest",
            )
        if not _SHA1.fullmatch(library_commit) or not source_path:
            raise CatalogValidationError(
                CatalogCode.SOURCE_INVALID,
                "recipe library publication identity is invalid",
            )
        if (release_version is None) != (release_released_at is None):
            raise CatalogValidationError(
                CatalogCode.RELEASE_INVALID,
                "recipe library release metadata is invalid",
            )
        if (
            release_version is not None
            and release_released_at is not None
            and (
                _RELEASE_VERSION.fullmatch(release_version) is None
                or date.fromisoformat(release_released_at).isoformat()
                != release_released_at
            )
        ):
            raise CatalogValidationError(
                CatalogCode.RELEASE_INVALID,
                "recipe library release metadata is invalid",
            )
        if package_handle is not None:
            _package_handle_metadata(
                package_handle,
                recipe_digest=expected_content_sha256,
                package_sha256=package_sha256,
            )
        actor = _actor(actor)
        with self._sessions.begin() as session:
            for value in dependency_documents:
                self._upsert_canonical_document(session, value, actor=actor)
            revision = self._upsert_canonical_document(session, document, actor=actor)
            self._select_imported_recipe_head(session, revision)
            projected = read_catalog_projection(revision).model_dump(
                mode="json", exclude_none=False
            )
            projected.update(
                {
                    "publication_commit": library_commit,
                    "source_path": source_path,
                    "package_sha256": package_sha256,
                    "source_bundle_sha256": source_bundle_sha256,
                    "package_handle": _package_handle_metadata(
                        package_handle,
                        recipe_digest=expected_content_sha256,
                        package_sha256=package_sha256,
                    )
                    if package_handle is not None
                    else None,
                    "release_version": release_version,
                    "release_released_at": release_released_at,
                }
            )
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(
                    projected=write_catalog_projection(projected, kind=revision.kind)
                )
            )
            session.expire(revision, ["projected"])
            return _view(revision)

    def _select_imported_recipe_head(
        self, session: Session, revision: CatalogDocumentRevision
    ) -> None:
        """Select a retained recipe without reactivating its Model dependencies.

        New imports already move the head when resolving their candidate.  A
        retained digest must also become current when the imported catalog pins
        it again; its immutable revision and exact dependency bindings survive.
        """
        root = session.get(CatalogDocument, revision.document_id, with_for_update=True)
        if root is None:
            raise CatalogValidationError(
                CatalogCode.DOCUMENT_MISSING, "catalog document is missing"
            )
        head = session.scalar(
            select(CatalogDocumentHead)
            .where(
                CatalogDocumentHead.kind == root.kind,
                CatalogDocumentHead.publisher == root.publisher,
                CatalogDocumentHead.slug == root.slug,
            )
            .with_for_update()
        )
        if head is None:
            raise CatalogValidationError(
                CatalogCode.HEAD_MISSING, "catalog document head is missing"
            )
        if head.active_revision_id == revision.id:
            return
        if head.candidate_revision_id is not None:
            CatalogEntityService(session, clock=self._clock).fail_candidate(
                root.id,
                reason=f"Superseded by imported recipe {revision.content_digest}.",
            )
        head.active_revision_id = revision.id
        head.generation += 1
        recipe = read_catalog_document(revision)
        if not isinstance(recipe, RecipeDefinition):
            raise CatalogValidationError(
                CatalogCode.RECIPE_INVALID, "catalog revision is not a recipe"
            )
        root.title = recipe.metadata.title
        root.updated_at = self._clock()

    def _upsert_canonical_document(
        self, session: Session, document: Mapping[str, object], *, actor: str
    ) -> CatalogDocumentRevision:
        parsed = (
            read_model(document)
            if document.get("kind") == "model"
            else read_recipe(document)
        )
        kind = str(parsed.kind)
        digest = document_sha256(document)
        identity = parsed.identity
        existing = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == kind,
                CatalogDocumentRevision.publisher == identity.publisher,
                CatalogDocumentRevision.slug == identity.slug,
                CatalogDocumentRevision.content_digest == digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if existing is not None:
            return existing
        service = CatalogEntityService(
            session, clock=self._clock, cursors=self._cursors
        )
        root = session.scalar(
            select(CatalogDocument)
            .where(
                CatalogDocument.kind == kind,
                CatalogDocument.publisher == identity.publisher,
                CatalogDocument.slug == identity.slug,
            )
            .with_for_update()
        )
        if root is None:
            candidate = service.create_draft(document, actor=actor)
        else:
            head = session.scalar(
                select(CatalogDocumentHead)
                .where(
                    CatalogDocumentHead.kind == kind,
                    CatalogDocumentHead.publisher == identity.publisher,
                    CatalogDocumentHead.slug == identity.slug,
                )
                .with_for_update()
            )
            if head is not None and head.candidate_revision_id is not None:
                service.fail_candidate(
                    root.id, reason=f"Superseded by imported {kind} {digest}."
                )
            latest = session.scalar(
                select(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.document_id == root.id)
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            )
            candidate = service.revise(
                root.id,
                document,
                actor=actor,
                expected_revision=latest.revision_number if latest else None,
            )
        return service.resolve(candidate.id, actor=actor)

    def resolve_recipe_revision(
        self, document: Mapping[str, object], *, actor: str
    ) -> str:
        try:
            recipe = read_recipe(document)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError(
                CatalogCode.DOCUMENT_INVALID, "recipe document is invalid"
            ) from error
        with self._sessions() as session:
            _resolve_recipe(session, recipe)
        return document_sha256(document)


def _get_active_recipe(
    session: Session, recipe_id: str
) -> CatalogDocumentRevision | None:
    return session.scalar(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.state == "active",
            or_(
                CatalogDocumentRevision.id == recipe_id,
                and_(
                    CatalogDocumentRevision.document_id == recipe_id,
                    active_head_revision(),
                ),
            ),
        )
    )


def _resolve_recipe(session: Session, recipe: RecipeDefinition) -> None:
    service = CatalogEntityService(
        session, clock=lambda: datetime.now(UTC), cursors=None
    )
    for selection in recipe.models:
        service.resolve_reference(selection.model)


def _view(revision: CatalogDocumentRevision) -> RecipeRevisionView:
    recipe = read_catalog_document(revision)
    if not isinstance(recipe, RecipeDefinition):
        raise CatalogValidationError(
            CatalogCode.RECIPE_INVALID, "catalog revision is not a recipe"
        )
    return RecipeRevisionView(
        id=revision.id,
        recipe_id=revision.document_id,
        slug=revision.slug,
        title=recipe.metadata.title,
        description=recipe.metadata.description,
        source_kind="recipe_library",
        revision_number=revision.revision_number,
        lifecycle="resolved" if revision.state == "active" else revision.state,
        schema_version=revision.schema_version,
        document=dict(revision.document),
        content_sha256=revision.content_digest,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _node_count(document: Mapping[str, object]) -> int | None:
    try:
        return recipe_topology(document).node_count
    except (RecipeRuntimeSpecError, TypeError, ValueError):
        return None


def _projected_package_sha256(row: CatalogDocumentRevision) -> str | None:
    try:
        projection = read_catalog_projection(row)
    except Exception:  # noqa: BLE001 - an unreadable projection is re-imported
        return None
    value = getattr(projection, "package_sha256", None)
    return value if isinstance(value, str) else None


def _release_version(document: Mapping[str, object]) -> str | None:
    release = document.get("release")
    version = release.get("version") if isinstance(release, Mapping) else None
    return (
        version
        if isinstance(version, str) and _RELEASE_VERSION.fullmatch(version)
        else None
    )


def _package_handle_metadata(
    handle: object, *, recipe_digest: str, package_sha256: str | None
) -> dict[str, object]:
    fields = (
        "publication_commit",
        "source_commit",
        "package_sha256",
        "package_size",
        "package_path",
        "recipe_content_sha256",
        "archive_path",
        "closure_path",
    )
    values: dict[str, object] = {
        field: getattr(handle, field, None) for field in fields
    }
    for field in ("package_path", "archive_path", "closure_path"):
        if values[field] is not None:
            values[field] = str(values[field])
    if package_sha256 is not None and values["package_sha256"] != package_sha256:
        raise CatalogValidationError(
            CatalogCode.PACKAGE_HANDLE_INVALID,
            "recipe package handle digest is invalid",
        )
    if (
        not isinstance(values["package_sha256"], str)
        or _SHA256.fullmatch(values["package_sha256"]) is None
    ):
        raise CatalogValidationError(
            CatalogCode.PACKAGE_HANDLE_INVALID,
            "recipe package handle digest is invalid",
        )
    package_size = values["package_size"]
    if (
        values["recipe_content_sha256"] != recipe_digest
        or not isinstance(package_size, int)
        or package_size <= 0
    ):
        raise CatalogValidationError(
            CatalogCode.PACKAGE_HANDLE_INVALID,
            "recipe package handle identity is invalid",
        )
    if not all(
        isinstance(values[field], str) and values[field]
        for field in (
            "publication_commit",
            "source_commit",
            "package_path",
            "archive_path",
            "closure_path",
        )
    ):
        raise CatalogValidationError(
            CatalogCode.PACKAGE_HANDLE_INVALID,
            "recipe package handle closure is invalid",
        )
    return values


def _actor(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise CatalogValidationError(CatalogCode.ACTOR, "catalog actor is invalid")
    return normalized
