"""Immutable catalog entity authoring and exact revision lookup."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .auth import CursorCodec
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
        cursors: CursorCodec,
    ) -> None:
        self._session = sessions if isinstance(sessions, Session) else None
        self._sessions = None if self._session is not None else sessions
        self._clock = clock
        self._cursors = cursors

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

    def resolve(
        self,
        entity_or_revision_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> CatalogEntityRevision:
        actor = _actor(actor)
        try:
            with self._write() as session:
                entity = self._entity(session, entity_or_revision_id, for_update=True)
                latest = self._latest(session, entity.id)
                if latest is None:
                    raise KeyError(entity_or_revision_id)
                if entity.id == entity_or_revision_id:
                    source = latest
                else:
                    source = session.get(CatalogEntityRevision, entity_or_revision_id)
                    if source is None or source.entity_id != entity.id:
                        raise KeyError(entity_or_revision_id)
                accepted_revisions = {latest.revision_number}
                if latest.lifecycle == "resolved":
                    accepted_revisions.add(latest.revision_number - 1)
                if (
                    expected_revision is not None
                    and expected_revision not in accepted_revisions
                ):
                    raise CatalogConflict(
                        "catalog.stale_entity_revision",
                        "catalog entity revision changed",
                    )
                if source.lifecycle == "resolved":
                    return source
                clean = self._validated_document(source.document)
                digest = catalog_content_sha256(clean)
                existing = session.scalar(
                    select(CatalogEntityRevision).where(
                        CatalogEntityRevision.entity_id == source.entity_id,
                        CatalogEntityRevision.content_sha256 == digest,
                        CatalogEntityRevision.lifecycle == "resolved",
                    ).order_by(CatalogEntityRevision.revision_number.desc())
                )
                if existing is not None and latest.id != source.id:
                    return existing
                if latest.id != source.id:
                    raise CatalogConflict(
                        "catalog.stale_entity_revision",
                        "catalog entity revision changed",
                    )
                self._validate_lineage(session, clean)
                revision = CatalogEntityRevision(
                    entity_id=source.entity_id,
                    revision_number=latest.revision_number + 1,
                    lifecycle="resolved",
                    schema_version=int(clean["schema_version"]),
                    document=clean,
                    content_sha256=digest,
                    created_by=actor,
                    created_at=self._clock(),
                    entity=entity,
                )
                session.add(revision)
                session.flush()
                return revision
        except IntegrityError as error:
            raise CatalogConflict(
                "catalog.entity_resolution_conflict",
                "catalog entity resolution conflicted with another writer",
            ) from error

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
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[CatalogEntityRevision], str | None]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise CatalogValidationError("catalog.limit", "catalog limit is invalid")
        try:
            normalized_kind = CatalogKind(kind).value if kind is not None else None
        except ValueError as error:
            raise CatalogValidationError(
                "catalog.kind", "catalog entity kind is invalid"
            ) from error
        context = {"kind": normalized_kind, "publisher": publisher}
        boundary: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                decoded = self._cursors.decode(
                    cursor,
                    resource="catalog-entities",
                    order="created-at-desc/id-desc/v1",
                    context=context,
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 2
                    or not all(isinstance(item, str) for item in decoded)
                ):
                    raise ValueError
                boundary = (datetime.fromisoformat(decoded[0]), decoded[1])
            except (UnicodeError, ValueError, TypeError):
                raise CatalogValidationError(
                    "catalog.cursor", "catalog cursor is invalid"
                ) from None
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
                .order_by(CatalogEntity.created_at.desc(), CatalogEntity.id.desc())
            )
            if normalized_kind is not None:
                statement = statement.where(CatalogEntity.kind == normalized_kind)
            if publisher is not None:
                statement = statement.where(CatalogEntity.publisher == publisher)
            if boundary is not None:
                created_at, entity_id = boundary
                statement = statement.where(
                    or_(
                        CatalogEntity.created_at < created_at,
                        and_(
                            CatalogEntity.created_at == created_at,
                            CatalogEntity.id < entity_id,
                        ),
                    )
                )
            rows = list(session.scalars(statement.limit(limit + 1)).all())
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            entity = page[-1].entity
            next_cursor = self._cursors.encode(
                resource="catalog-entities",
                order="created-at-desc/id-desc/v1",
                context=context,
                boundary=[_aware(entity.created_at).isoformat(), entity.id],
            )
        return page, next_cursor

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
        statement = self._entity_statement(entity_or_revision_id, for_update=for_update)
        entity = session.scalar(statement)
        if entity is not None:
            return entity
        revision = session.get(CatalogEntityRevision, entity_or_revision_id)
        if revision is None:
            raise KeyError(entity_or_revision_id)
        if for_update:
            locked = session.scalar(
                self._entity_statement(revision.entity_id, for_update=True)
            )
            if locked is None:
                raise KeyError(entity_or_revision_id)
            return locked
        return revision.entity

    @staticmethod
    def _entity_statement(entity_id: str, *, for_update: bool):
        statement = select(CatalogEntity).where(CatalogEntity.id == entity_id)
        return statement.with_for_update() if for_update else statement

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
            .order_by(CatalogEntityRevision.revision_number.desc())
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
        elif kind is CatalogKind.RUNTIME_DISTRIBUTION:
            self._lookup_document_reference(
                session,
                document,
                "implements_harness",
                CatalogKind.EXECUTION_HARNESS,
            )
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


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "CatalogConflict",
    "CatalogEntityService",
    "CatalogError",
    "CatalogValidationError",
]
