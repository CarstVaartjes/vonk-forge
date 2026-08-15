"""Immutable catalog entity authoring and exact revision lookup."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .catalog_contract import (
    CatalogContractError,
    CatalogKind,
    CatalogReference,
    catalog_content_sha256,
    parse_catalog_reference,
    validate_catalog_document,
)
from .catalog_repository import sensitive_document_path
from .models import CatalogEntity, CatalogEntityRevision


class CatalogError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class CatalogConflict(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


class CatalogEntityService:
    """Store draft revisions and resolve only exact immutable dependencies."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._session = sessions if isinstance(sessions, Session) else None
        self._sessions = None if self._session is not None else sessions
        self._clock = clock

    @contextmanager
    def _read(self) -> Iterator[Session]:
        if self._session is not None:
            self._session.flush()
            yield self._session
            return
        assert self._sessions is not None
        with self._sessions() as session:
            yield session

    @contextmanager
    def _write(self) -> Iterator[Session]:
        if self._session is not None:
            yield self._session
            self._session.flush()
            return
        assert self._sessions is not None
        with self._sessions.begin() as session:
            yield session

    def create_draft(
        self, document: Mapping[str, object], *, actor: str
    ) -> CatalogEntityRevision:
        clean = self._validated_document(document)
        kind, publisher, slug, title = _identity(clean)
        now = self._clock()
        entity = CatalogEntity(
            kind=kind.value,
            publisher=publisher,
            slug=slug,
            title=title,
            created_by=_actor(actor),
            created_at=now,
            updated_at=now,
        )
        revision = CatalogEntityRevision(
            entity_id="",
            revision_number=1,
            lifecycle="draft",
            schema_version=int(clean["schema_version"]),
            document=clean,
            content_sha256=None,
            created_by=_actor(actor),
            created_at=now,
        )
        try:
            with self._write() as session:
                session.add(entity)
                session.flush()
                revision.entity_id = entity.id
                revision.entity = entity
                session.add(revision)
                session.flush()
        except IntegrityError as error:
            raise CatalogConflict(
                "catalog.entity_exists", "catalog entity identity already exists"
            ) from error
        return revision

    def revise(
        self,
        entity_id: str,
        document: Mapping[str, object],
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> CatalogEntityRevision:
        clean = self._validated_document(document)
        with self._write() as session:
            entity = self._entity(session, entity_id, for_update=True)
            kind, publisher, slug, title = _identity(clean)
            if (kind.value, publisher, slug) != (
                entity.kind,
                entity.publisher,
                entity.slug,
            ):
                raise CatalogValidationError(
                    "catalog.identity_changed",
                    "catalog entity revisions cannot change identity",
                )
            latest = self._latest(session, entity.id)
            if latest is None:
                raise KeyError(entity.id)
            if (
                expected_revision is not None
                and latest.revision_number != expected_revision
            ):
                raise CatalogConflict(
                    "catalog.stale_entity_revision", "catalog entity revision changed"
                )
            if latest.lifecycle == "deprecated":
                raise CatalogConflict(
                    "catalog.entity_deprecated",
                    "deprecated catalog entity cannot be revised",
                )
            now = self._clock()
            revision = CatalogEntityRevision(
                entity_id=entity.id,
                revision_number=latest.revision_number + 1,
                lifecycle="draft",
                schema_version=int(clean["schema_version"]),
                document=clean,
                content_sha256=None,
                created_by=_actor(actor),
                created_at=now,
                entity=entity,
            )
            entity.title = title
            entity.updated_at = now
            session.add(revision)
            session.flush()
            return revision

    def resolve(self, revision_id: str, *, actor: str) -> CatalogEntityRevision:
        actor = _actor(actor)
        with self._write() as session:
            source = session.get(CatalogEntityRevision, revision_id)
            if source is None:
                raise KeyError(revision_id)
            if source.lifecycle == "resolved":
                return source
            clean = self._validated_document(source.document)
            digest = catalog_content_sha256(clean)
            existing = session.scalar(
                select(CatalogEntityRevision).where(
                    CatalogEntityRevision.entity_id == source.entity_id,
                    CatalogEntityRevision.content_sha256 == digest,
                    CatalogEntityRevision.lifecycle == "resolved",
                )
            )
            if existing is not None:
                return existing
            latest = self._latest(session, source.entity_id)
            if latest is None or latest.id != source.id:
                raise CatalogConflict(
                    "catalog.stale_entity_revision", "catalog entity revision changed"
                )
            self._validate_lineage(session, clean)
            revision = CatalogEntityRevision(
                entity_id=source.entity_id,
                revision_number=source.revision_number + 1,
                lifecycle="resolved",
                schema_version=int(clean["schema_version"]),
                document=clean,
                content_sha256=digest,
                created_by=actor,
                created_at=self._clock(),
                entity=source.entity,
            )
            session.add(revision)
            session.flush()
            return revision

    def lookup_exact(
        self,
        kind: CatalogKind | str,
        publisher: str,
        slug: str,
        content_sha256: str,
    ) -> CatalogEntityRevision:
        try:
            normalized_kind = CatalogKind(kind)
        except ValueError as error:
            raise CatalogValidationError(
                "catalog.kind", "catalog entity kind is invalid"
            ) from error
        reference = CatalogReference(normalized_kind, publisher, slug, content_sha256)
        with self._read() as session:
            return self._lookup_exact(session, reference)

    def list_entities(
        self,
        *,
        kind: CatalogKind | str | None = None,
        publisher: str | None = None,
    ) -> list[CatalogEntityRevision]:
        normalized_kind = CatalogKind(kind).value if kind is not None else None
        latest_numbers = (
            select(
                CatalogEntityRevision.entity_id,
                func.max(CatalogEntityRevision.revision_number).label(
                    "revision_number"
                ),
            )
            .group_by(CatalogEntityRevision.entity_id)
            .subquery()
        )
        with self._read() as session:
            statement = (
                select(CatalogEntityRevision)
                .join(CatalogEntity)
                .join(
                    latest_numbers,
                    and_(
                        latest_numbers.c.entity_id == CatalogEntityRevision.entity_id,
                        latest_numbers.c.revision_number
                        == CatalogEntityRevision.revision_number,
                    ),
                )
                .order_by(CatalogEntity.updated_at.desc(), CatalogEntity.id.desc())
            )
            if normalized_kind is not None:
                statement = statement.where(CatalogEntity.kind == normalized_kind)
            if publisher is not None:
                statement = statement.where(CatalogEntity.publisher == publisher)
            return list(session.scalars(statement).all())

    def get_entity(self, entity_id: str) -> CatalogEntityRevision:
        with self._read() as session:
            entity = self._entity(session, entity_id)
            revision = self._latest(session, entity.id)
            if revision is None:
                raise KeyError(entity_id)
            return revision

    def _entity(
        self, session: Session, entity_or_revision_id: str, *, for_update: bool = False
    ) -> CatalogEntity:
        statement = select(CatalogEntity).where(
            CatalogEntity.id == entity_or_revision_id
        )
        if for_update:
            statement = statement.with_for_update()
        entity = session.scalar(statement)
        if entity is not None:
            return entity
        revision = session.get(CatalogEntityRevision, entity_or_revision_id)
        if revision is None:
            raise KeyError(entity_or_revision_id)
        return revision.entity

    def _latest(self, session: Session, entity_id: str) -> CatalogEntityRevision | None:
        return session.scalar(
            select(CatalogEntityRevision)
            .where(CatalogEntityRevision.entity_id == entity_id)
            .order_by(CatalogEntityRevision.revision_number.desc())
            .limit(1)
        )

    def _lookup_exact(
        self, session: Session, reference: CatalogReference
    ) -> CatalogEntityRevision:
        revision = session.scalar(
            select(CatalogEntityRevision)
            .join(CatalogEntity)
            .where(
                CatalogEntity.kind == reference.kind.value,
                CatalogEntity.publisher == reference.publisher,
                CatalogEntity.slug == reference.slug,
                CatalogEntityRevision.content_sha256 == reference.content_sha256,
                CatalogEntityRevision.lifecycle == "resolved",
            )
        )
        if revision is None:
            raise CatalogConflict(
                f"catalog.exact_{reference.kind.value}_required",
                f"exact {reference.kind.value} revision is not resolved",
            )
        return revision

    def _validate_lineage(
        self, session: Session, document: Mapping[str, object]
    ) -> None:
        kind = CatalogKind(document["kind"])
        if kind is CatalogKind.MODEL:
            self._lookup_document_reference(
                session, document, "model_group", CatalogKind.MODEL_GROUP
            )
        elif kind is CatalogKind.MODEL_VERSION:
            model_revision = self._lookup_document_reference(
                session, document, "model", CatalogKind.MODEL
            )
            self._lookup_document_reference(
                session,
                model_revision.document,
                "model_group",
                CatalogKind.MODEL_GROUP,
            )
        elif kind is CatalogKind.EXECUTION_HARNESS:
            self._lookup_document_reference(session, document, "source_bundle")
        elif kind is CatalogKind.PATCH_BUNDLE:
            self._lookup_document_reference(
                session, document, "applies_to", CatalogKind.RUNTIME_DISTRIBUTION
            )

    def _lookup_document_reference(
        self,
        session: Session,
        document: Mapping[str, object],
        field: str,
        expected_kind: CatalogKind | None = None,
    ) -> CatalogEntityRevision:
        value = document.get(field)
        if not isinstance(value, Mapping):
            raise CatalogValidationError(
                "catalog.reference", f"catalog {field} reference is invalid"
            )
        try:
            reference = parse_catalog_reference(value, expected_kind=expected_kind)
        except CatalogContractError as error:
            raise CatalogValidationError(error.code, error.detail) from error
        return self._lookup_exact(session, reference)

    def _validated_document(self, document: Mapping[str, object]) -> dict[str, object]:
        sensitive = sensitive_document_path(document)
        if sensitive is not None:
            raise CatalogValidationError(
                "catalog.sensitive_field",
                f"sensitive field is forbidden at {sensitive}",
            )
        clean: dict[str, object] = copy.deepcopy(dict(document))
        try:
            validate_catalog_document(clean)
        except CatalogContractError as error:
            raise CatalogValidationError(
                error.code, f"{error.path}: {error.detail}"
            ) from error
        return clean


def _identity(
    document: Mapping[str, object],
) -> tuple[CatalogKind, str, str, str]:
    identity = document["identity"]
    metadata = document["metadata"]
    assert isinstance(identity, Mapping)
    assert isinstance(metadata, Mapping)
    return (
        CatalogKind(document["kind"]),
        str(identity["publisher"]),
        str(identity["slug"]),
        str(metadata["title"]),
    )


def _actor(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise CatalogValidationError("catalog.actor", "catalog actor is invalid")
    return normalized


__all__ = [
    "CatalogConflict",
    "CatalogEntityService",
    "CatalogError",
    "CatalogValidationError",
]
