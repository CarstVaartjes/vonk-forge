"""Canonical Model and Recipe catalog persistence."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import BinaryIO

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .auth import CursorCodec
from .catalog_entities import (
    CatalogConflict,
    CatalogDocumentRevision,
    CatalogEntityService,
    CatalogError,
    CatalogValidationError,
)
from .models import (
    CatalogDocument,
    CatalogDocumentHead,
    RecipeSourceBundle,
)
from .schema_resources import read_runtime_schema
from .source_bundles import SourceBundleError, SourceBundleStore

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_REQUIRED_TEST_CHECKS = frozenset(
    {"container.started", "endpoint.healthy", "inference.completed"}
)


@dataclass(frozen=True, slots=True)
class RecipeDraftInput:
    """Retained as a non-authoritative input type for callers being retired."""

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
class RecipeCatalogLocalRevision:
    recipe_id: str
    source_kind: str
    publisher: str
    slug: str
    revision_number: int
    content_sha256: str | None
    release_version: str | None


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
        source_bundles: SourceBundleStore | None = None,
    ) -> None:
        del repository
        self._sessions = sessions
        self._clock = clock
        self._source_bundles = source_bundles
        self._cursors = cursors
        self.entities = CatalogEntityService(sessions, clock=clock, cursors=cursors)

    def store_source_bundle(
        self, expected_sha256: str, payload: BinaryIO, actor: str
    ) -> SourceBundleView:
        del actor
        if self._source_bundles is None:
            raise CatalogError("bundle.storage_unavailable", "source bundle storage is unavailable")
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
            raise CatalogConflict("bundle.storage_conflict", "source bundle metadata conflicts") from error
        return SourceBundleView(
            sha256=row.sha256,
            archive_bytes=row.archive_bytes,
            total_bytes=row.total_bytes,
            file_count=row.file_count,
            files=tuple(item.path for item in manifest.files),
        )

    def read_source_bundle(self, sha256: str) -> bytes:
        if self._source_bundles is None:
            raise CatalogError("bundle.storage_unavailable", "source bundle storage is unavailable")
        with self._sessions() as session:
            row = session.get(RecipeSourceBundle, sha256)
            if row is None:
                raise KeyError(sha256)
            expected = (row.archive_bytes, row.total_bytes, row.file_count)
        try:
            stored = self._source_bundles.get(sha256)
        except SourceBundleError as error:
            raise CatalogValidationError(error.code, error.detail) from error
        observed = (len(stored.archive), stored.manifest.total_bytes, len(stored.manifest.files))
        if observed != expected:
            raise CatalogValidationError("bundle.metadata_mismatch", "source bundle storage does not match its database metadata")
        return stored.archive

    def get_recipe(self, recipe_id: str) -> RecipeRevisionView:
        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    (CatalogDocumentRevision.document_id == recipe_id)
                    | (CatalogDocumentRevision.id == recipe_id),
                )
            )
        if revision is None:
            raise KeyError(recipe_id)
        return _view(revision)

    def recipe_catalog_local_revisions(self, slugs: Sequence[str]) -> dict[str, RecipeCatalogLocalRevision]:
        requested = sorted(set(slugs))
        if len(requested) > 256 or any(not _SLUG.fullmatch(slug) for slug in requested):
            raise CatalogValidationError("catalog.identities", "catalog recipe identities are invalid")
        if not requested:
            return {}
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.slug.in_(requested),
                    )
                )
            )
        result: dict[str, RecipeCatalogLocalRevision] = {}
        for row in rows:
            result[row.slug] = RecipeCatalogLocalRevision(
                recipe_id=row.document_id,
                source_kind="recipe_library",
                publisher=row.publisher,
                slug=row.slug,
                revision_number=row.revision_number,
                content_sha256=row.content_digest,
                release_version=_release_version(row.document),
            )
        return result

    def import_catalog_models(self, actor: str, documents: Sequence[Mapping[str, object]]) -> int:
        actor = _actor(actor)
        try:
            models = [ModelDefinition.model_validate(value) for value in documents]
        except (TypeError, ValueError) as error:
            raise CatalogValidationError("recipe_library.model_document_invalid", "catalog index model documents are invalid") from error
        with self._sessions.begin() as session:
            for model in models:
                self._upsert_canonical_document(session, model.model_dump(mode="json"), actor=actor)
        return len(models)

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
            recipe = RecipeDefinition.model_validate(document)
            models = [ModelDefinition.model_validate(value) for value in dependency_documents]
        except (TypeError, ValueError) as error:
            raise CatalogValidationError("recipe_library.document_invalid", "recipe library package must contain a canonical recipe and model snapshots") from error
        actual = content_sha256(recipe)
        if actual != expected_content_sha256:
            raise CatalogValidationError("recipe_library.hash_mismatch", "recipe content does not match the supplied digest")
        if not _SHA1.fullmatch(library_commit) or not source_path:
            raise CatalogValidationError("recipe_library.source_invalid", "recipe library publication identity is invalid")
        if (release_version is None) != (release_released_at is None):
            raise CatalogValidationError("recipe_library.release_invalid", "recipe library release metadata is invalid")
        if release_version is not None and (_RELEASE_VERSION.fullmatch(release_version) is None or date.fromisoformat(release_released_at).isoformat() != release_released_at):
            raise CatalogValidationError("recipe_library.release_invalid", "recipe library release metadata is invalid")
        if package_handle is not None:
            _package_handle_metadata(package_handle, recipe=recipe, package_sha256=package_sha256)
        actor = _actor(actor)
        with self._sessions.begin() as session:
            for model in models:
                self._upsert_canonical_document(session, model.model_dump(mode="json"), actor=actor)
            revision = self._upsert_canonical_document(session, recipe.model_dump(mode="json"), actor=actor)
            projected = dict(revision.projected or {})
            projected.update(
                {
                    "publication_commit": library_commit,
                    "source_path": source_path,
                    "package_sha256": package_sha256,
                    "source_bundle_sha256": source_bundle_sha256,
                    "package_handle": _package_handle_metadata(package_handle, recipe=recipe, package_sha256=package_sha256) if package_handle is not None else None,
                    "release_version": release_version,
                    "release_released_at": release_released_at,
                }
            )
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(projected=projected)
            )
            session.expire(revision, ["projected"])
            return _view(revision)

    def _upsert_canonical_document(self, session: Session, document: Mapping[str, object], *, actor: str) -> CatalogDocumentRevision:
        parsed = ModelDefinition.model_validate(document) if document.get("kind") == "model" else RecipeDefinition.model_validate(document)
        kind = str(parsed.kind)
        digest = content_sha256(parsed)
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
        service = CatalogEntityService(session, clock=self._clock, cursors=self._cursors)
        root = session.scalar(
            select(CatalogDocument).where(
                CatalogDocument.kind == kind,
                CatalogDocument.publisher == identity.publisher,
                CatalogDocument.slug == identity.slug,
            ).with_for_update()
        )
        if root is None:
            candidate = service.create_draft(parsed.model_dump(mode="json"), actor=actor)
        else:
            head = session.scalar(
                select(CatalogDocumentHead).where(
                    CatalogDocumentHead.kind == kind,
                    CatalogDocumentHead.publisher == identity.publisher,
                    CatalogDocumentHead.slug == identity.slug,
                ).with_for_update()
            )
            if head is not None and head.candidate_revision_id is not None:
                service.fail_candidate(root.id)
            latest = session.scalar(
                select(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.document_id == root.id)
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            )
            candidate = service.revise(root.id, parsed.model_dump(mode="json"), actor=actor, expected_revision=latest.revision_number if latest else None)
        return service.resolve(candidate.id, actor=actor)

    def resolve_recipe_revision(self, document: Mapping[str, object], *, actor: str) -> str:
        try:
            recipe = RecipeDefinition.model_validate(document)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError("catalog.document_invalid", "recipe document is invalid") from error
        with self._sessions() as session:
            return self._resolve_recipe(session, recipe, actor=actor)

    def attach_test_report(self, recipe_id: str, report: Mapping[str, object], actor: str) -> dict[str, object]:
        del actor
        clean = copy.deepcopy(dict(report))
        errors = sorted(_test_report_validator().iter_errors(clean), key=lambda error: tuple(str(part) for part in error.absolute_path))
        if errors:
            raise CatalogValidationError("catalog.test_report_invalid", "test report is invalid")
        with self._sessions.begin() as session:
            revision = _get_active_recipe(session, recipe_id)
            if revision is None:
                raise KeyError(recipe_id)
            if clean.get("recipe_sha256") != revision.content_digest:
                raise CatalogValidationError("catalog.test_report_recipe_mismatch", "test report does not match this recipe revision")
            projected = dict(revision.projected or {})
            projected["test_report"] = clean
            revision.projected = projected
            session.flush()
        return clean

    def publication_export(self, recipe_id: str, target_publisher: str) -> dict[str, object]:
        if not _SLUG.fullmatch(target_publisher):
            raise CatalogValidationError("catalog.publisher", "target publisher namespace is invalid")
        with self._sessions() as session:
            revision = _get_active_recipe(session, recipe_id)
            if revision is None:
                raise KeyError(recipe_id)
            report = (revision.projected or {}).get("test_report")
            if not isinstance(report, dict):
                raise CatalogConflict("catalog.test_report_required", "attach a passing local test report before publication export")
            recipe = copy.deepcopy(revision.document)
        identity = recipe["identity"]
        if isinstance(identity, dict):
            identity["publisher"] = target_publisher
        return {"recipe": recipe, "test_report": report}

def _get_active_recipe(session: Session, recipe_id: str) -> CatalogDocumentRevision | None:
    return session.scalar(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.state == "active",
            (CatalogDocumentRevision.document_id == recipe_id) | (CatalogDocumentRevision.id == recipe_id),
        )
    )


def _resolve_recipe(session: Session, recipe: RecipeDefinition, *, actor: str) -> str:
    service = CatalogEntityService(session, clock=lambda: datetime.now(UTC), cursors=None)
    for selection in recipe.models:
        service.resolve_reference(selection.model)
    return content_sha256(recipe)


def _view(revision: CatalogDocumentRevision) -> RecipeRevisionView:
    metadata = revision.document.get("metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return RecipeRevisionView(
        id=revision.id,
        recipe_id=revision.document_id,
        slug=revision.slug,
        title=str(metadata.get("title", revision.slug)),
        description=str(metadata.get("description", "")),
        source_kind="recipe_library",
        revision_number=revision.revision_number,
        lifecycle="resolved" if revision.state == "active" else revision.state,
        schema_version=revision.schema_version,
        document=copy.deepcopy(revision.document),
        content_sha256=revision.content_digest,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _release_version(document: Mapping[str, object]) -> str | None:
    release = document.get("release")
    history = release.get("history") if isinstance(release, Mapping) else None
    current = history[0] if isinstance(history, list) and history else None
    version = current.get("version") if isinstance(current, Mapping) else None
    return version if isinstance(version, str) and _RELEASE_VERSION.fullmatch(version) else None


def _package_handle_metadata(handle: object, *, recipe: RecipeDefinition, package_sha256: str | None) -> dict[str, object]:
    fields = ("publication_commit", "source_commit", "package_sha256", "package_size", "package_path", "recipe_content_sha256", "archive_path", "closure_path")
    values = {field: getattr(handle, field, None) for field in fields}
    for field in ("package_path", "archive_path", "closure_path"):
        if values[field] is not None:
            values[field] = str(values[field])
    if package_sha256 is not None and values["package_sha256"] != package_sha256:
        raise CatalogValidationError("recipe_library.package_handle_invalid", "recipe package handle digest is invalid")
    if not isinstance(values["package_sha256"], str) or _SHA256.fullmatch(values["package_sha256"]) is None:
        raise CatalogValidationError("recipe_library.package_handle_invalid", "recipe package handle digest is invalid")
    if values["recipe_content_sha256"] != content_sha256(recipe) or not isinstance(values["package_size"], int) or values["package_size"] <= 0:
        raise CatalogValidationError("recipe_library.package_handle_invalid", "recipe package handle identity is invalid")
    if not all(isinstance(values[field], str) and values[field] for field in ("publication_commit", "source_commit", "package_path", "archive_path", "closure_path")):
        raise CatalogValidationError("recipe_library.package_handle_invalid", "recipe package handle closure is invalid")
    return values


@__import__("functools").lru_cache(maxsize=1)
def _test_report_validator() -> Draft202012Validator:
    schema = json.loads(read_runtime_schema("test-report-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _actor(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise CatalogValidationError("catalog.actor", "catalog actor is invalid")
    return normalized
