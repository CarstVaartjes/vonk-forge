"""PostgreSQL persistence for the two public catalog document contracts."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.resolver import validate_recipe_models

from .auth import CursorCodec
from .models import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)


class CatalogError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail
        super().__init__(detail)


class CatalogConflict(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


class CatalogEntityService:
    """Store immutable Model/Recipe revisions and switch active heads."""

    def __init__(self, sessions: Session | sessionmaker[Session], *, clock: Callable[[], datetime], cursors: CursorCodec | None = None) -> None:
        self._session = sessions if isinstance(sessions, Session) else None
        self._sessions = None if self._session is not None else sessions
        self._clock, self._cursors = clock, cursors

    @contextmanager
    def _write(self) -> Iterator[Session]:
        if self._session is not None:
            yield self._session
            self._session.flush()
            return
        assert self._sessions is not None
        with self._sessions.begin() as session:
            yield session

    @contextmanager
    def _read(self) -> Iterator[Session]:
        if self._session is not None:
            self._session.flush()
            yield self._session
            return
        assert self._sessions is not None
        with self._sessions() as session:
            yield session

    def create_draft(self, document: Mapping[str, object], *, actor: str) -> CatalogDocumentRevision:
        parsed, clean, kind, publisher, slug, title = _parse(document)
        now, actor = self._clock(), _actor(actor)
        try:
            with self._write() as session:
                root = CatalogDocument(kind=kind, publisher=publisher, slug=slug, title=title, created_by=actor, created_at=now, updated_at=now)
                session.add(root)
                session.flush()
                revision = _revision(root, parsed, clean, 1, actor, now)
                session.add(revision)
                session.flush()
                session.add(CatalogDocumentHead(kind=kind, publisher=publisher, slug=slug, candidate_revision_id=revision.id, generation=0))
                session.flush()
                return revision
        except IntegrityError as error:
            raise CatalogConflict("catalog.document_exists", "catalog document identity already exists") from error

    def revise(self, document_id: str, document: Mapping[str, object], *, actor: str, expected_revision: int | None = None) -> CatalogDocumentRevision:
        parsed, clean, kind, publisher, slug, title = _parse(document)
        now, actor = self._clock(), _actor(actor)
        with self._write() as session:
            root = session.scalar(select(CatalogDocument).where(CatalogDocument.id == document_id).with_for_update())
            if root is None:
                raise KeyError(document_id)
            if (root.kind, root.publisher, root.slug) != (kind, publisher, slug):
                raise CatalogValidationError("catalog.identity_changed", "document identity cannot change")
            head = _head(session, root)
            latest = session.scalar(select(CatalogDocumentRevision).where(CatalogDocumentRevision.document_id == root.id).order_by(CatalogDocumentRevision.revision_number.desc()).limit(1))
            if latest is None:
                raise CatalogValidationError("catalog.revision_missing", "document has no revision")
            if expected_revision is not None and latest.revision_number != expected_revision:
                raise CatalogConflict("catalog.stale_revision", "document revision changed")
            if head.candidate_revision_id is not None:
                raise CatalogConflict("catalog.candidate_exists", "document already has a pending candidate")
            revision = _revision(root, parsed, clean, latest.revision_number + 1, actor, now)
            session.add(revision)
            session.flush()
            head.candidate_revision_id, root.title, root.updated_at = revision.id, title, now
            session.flush()
            return revision

    def resolve(self, entity_or_revision_id: str, *, actor: str, expected_revision: int | None = None) -> CatalogDocumentRevision:
        del actor
        with self._write() as session:
            revision = session.get(CatalogDocumentRevision, entity_or_revision_id)
            root = session.get(CatalogDocument, revision.document_id, with_for_update=True) if revision else session.get(CatalogDocument, entity_or_revision_id, with_for_update=True)
            if root is None:
                raise KeyError(entity_or_revision_id)
            head = _head(session, root)
            if revision is None:
                revision = session.get(CatalogDocumentRevision, head.candidate_revision_id) if head.candidate_revision_id else None
            if revision is None:
                raise KeyError(entity_or_revision_id)
            if revision.state == "active":
                return revision
            if revision.id != head.candidate_revision_id:
                raise CatalogConflict("catalog.not_candidate", "only the current candidate can be activated")
            if expected_revision is not None and revision.revision_number != expected_revision:
                raise CatalogConflict("catalog.stale_revision", "document revision changed")
            if revision.kind == "recipe":
                self._bind_recipe_models(session, revision)
            revision.state, head.active_revision_id, head.candidate_revision_id = "active", revision.id, None
            head.generation += 1
            session.flush()
            return revision

    def fail_candidate(self, document_id: str, *, reason: str | None = None) -> None:
        with self._write() as session:
            root = session.get(CatalogDocument, document_id, with_for_update=True)
            if root is None:
                raise KeyError(document_id)
            head = _head(session, root)
            if head.candidate_revision_id is None:
                return
            candidate = session.get(CatalogDocumentRevision, head.candidate_revision_id)
            if candidate is not None:
                candidate.state = "failed"
                if reason:
                    candidate.projected = {**candidate.projected, "failure_reason": reason[:240]}
            head.candidate_revision_id = None

    def get_entity(self, entity_id: str) -> CatalogDocumentRevision:
        with self._read() as session:
            root = session.get(CatalogDocument, entity_id)
            if root is None:
                revision = session.get(CatalogDocumentRevision, entity_id)
                if revision is None:
                    raise KeyError(entity_id)
                return revision
            head = _head(session, root)
            revision = session.get(CatalogDocumentRevision, head.active_revision_id or head.candidate_revision_id)
            if revision is None:
                raise KeyError(entity_id)
            return revision

    def resolve_reference(self, reference: object) -> CatalogDocumentRevision:
        kind = getattr(getattr(reference, "kind", None), "value", getattr(reference, "kind", None))
        publisher, slug, digest = (getattr(reference, key, None) for key in ("publisher", "slug", "content_sha256"))
        if kind not in {"model", "recipe"} or not all(isinstance(value, str) for value in (publisher, slug, digest)):
            raise CatalogValidationError("catalog.reference", "only exact model/recipe references are supported")
        with self._read() as session:
            revision = session.scalar(select(CatalogDocumentRevision).where(CatalogDocumentRevision.kind == kind, CatalogDocumentRevision.publisher == publisher, CatalogDocumentRevision.slug == slug, CatalogDocumentRevision.content_digest == digest, CatalogDocumentRevision.state == "active").limit(1))
            if revision is None:
                raise CatalogValidationError("catalog.reference_missing", "exact referenced document is not active")
            return revision

    def _bind_recipe_models(self, session: Session, revision: CatalogDocumentRevision) -> None:
        recipe = RecipeDefinition.model_validate(revision.document)
        models, bindings = [], []
        artifact_inputs = []
        for selection in recipe.models:
            ref = selection.model
            model_revision = session.scalar(select(CatalogDocumentRevision).where(CatalogDocumentRevision.kind == "model", CatalogDocumentRevision.publisher == ref.publisher, CatalogDocumentRevision.slug == ref.slug, CatalogDocumentRevision.content_digest == ref.content_sha256, CatalogDocumentRevision.state == "active").limit(1))
            if model_revision is None:
                raise CatalogValidationError("catalog.model_reference_missing", f"model reference is missing: {ref.publisher}/{ref.slug}")
            model = ModelDefinition.model_validate(model_revision.document)
            models.append(model)
            if model_revision.artifact_key is None:
                raise CatalogValidationError("catalog.model_artifact_missing", f"model artifact projection is missing: {ref.publisher}/{ref.slug}")
            artifact_inputs.append({"selection_id": selection.id, "artifact_key": model_revision.artifact_key})
            bindings.append(CatalogRecipeModelReference(recipe_revision_id=revision.id, recipe_kind="recipe", selection_id=selection.id, model_revision_id=model_revision.id, model_kind="model", model_publisher=ref.publisher, model_slug=ref.slug, model_content_digest=ref.content_sha256))
        try:
            validate_recipe_models(recipe, models)
        except ValueError as error:
            raise CatalogValidationError("catalog.model_reference_invalid", str(error)) from error
        revision.artifact_key = _digest({"models": artifact_inputs})
        revision.execution_key = _digest({"execution": _execution_projection(recipe), "artifact_key": revision.artifact_key})
        revision.projected = {**revision.projected, "artifact_inputs": artifact_inputs}
        session.add_all(bindings)


def _parse(document: Mapping[str, object]) -> tuple[ModelDefinition | RecipeDefinition, dict[str, object], str, str, str, str]:
    try:
        raw = dict(document)
        parsed = ModelDefinition.model_validate(raw) if raw.get("kind") == "model" else RecipeDefinition.model_validate(raw)
    except Exception as error:
        raise CatalogValidationError("catalog.document_invalid", "document does not satisfy the public v2 contract") from error
    clean = parsed.model_dump(mode="json", exclude_unset=False, exclude_none=False)
    title = parsed.identity.model.title if isinstance(parsed, ModelDefinition) else parsed.metadata.title
    return parsed, clean, str(parsed.kind), parsed.identity.publisher, parsed.identity.slug, title


def _revision(root: CatalogDocument, parsed: ModelDefinition | RecipeDefinition, clean: dict[str, object], number: int, actor: str, now: datetime) -> CatalogDocumentRevision:
    artifact_key = download = installed = None
    if isinstance(parsed, ModelDefinition):
        files = [item.model_dump(mode="json") for item in parsed.files]
        artifact_key = _digest({"files": files, "format": parsed.format.model_dump(mode="json")})
        download, installed = parsed.download_bytes, parsed.installed_bytes
        projected = {"identity": parsed.identity.model_dump(mode="json"), "modalities": parsed.modalities, "artifact_count": len(parsed.files), "download_bytes": download, "installed_bytes": installed}
    else:
        projected = {"title": parsed.metadata.title, "description": parsed.metadata.description, "tags": parsed.metadata.tags, "runtime_engine": parsed.runtime.engine, "topology": parsed.topology.model_dump(mode="json")}
    return CatalogDocumentRevision(document_id=root.id, kind=str(parsed.kind), publisher=parsed.identity.publisher, slug=parsed.identity.slug, revision_number=number, schema_version=2, state="candidate", document=copy.deepcopy(clean), content_digest=content_sha256(parsed), artifact_key=artifact_key, execution_key=_digest(_execution_projection(parsed)), download_bytes=download, installed_bytes=installed, projected=projected, created_by=actor, created_at=now)


def _execution_projection(parsed: ModelDefinition | RecipeDefinition) -> object:
    if isinstance(parsed, ModelDefinition):
        return {"artifact_key": _digest({"files": [item.model_dump(mode="json") for item in parsed.files], "format": parsed.format.model_dump(mode="json")})}
    return {"execution": parsed.execution.model_dump(mode="json"), "runtime": parsed.runtime.model_dump(mode="json"), "topology": parsed.topology.model_dump(mode="json"), "interfaces": [item.model_dump(mode="json") for item in parsed.interfaces], "settings": parsed.settings.model_dump(mode="json")}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _head(session: Session, root: CatalogDocument) -> CatalogDocumentHead:
    head = session.scalar(select(CatalogDocumentHead).where(CatalogDocumentHead.kind == root.kind, CatalogDocumentHead.publisher == root.publisher, CatalogDocumentHead.slug == root.slug).with_for_update())
    if head is None:
        raise CatalogValidationError("catalog.head_missing", "catalog document head is missing")
    return head


def _actor(actor: str) -> str:
    return actor if isinstance(actor, str) and actor else "system"


__all__ = ["CatalogConflict", "CatalogDocument", "CatalogDocumentHead", "CatalogDocumentRevision", "CatalogEntityService", "CatalogError", "CatalogRecipeModelReference", "CatalogValidationError"]
