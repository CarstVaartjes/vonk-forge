"""PostgreSQL persistence for the two public catalog document contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    CatalogCode,
    InvalidRequestError,
    InvalidRequestReason,
    canonical_message,
)
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.resolver import validate_recipe_models

from .auth import CursorCodec
from .catalog_revision_contract import (
    CatalogRevisionContractError,
    PrebuiltImage,
    RecipeRevisionProjection,
    read_catalog_document,
    read_catalog_projection,
    write_catalog_projection,
)
from .models import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)

_LOGGER = logging.getLogger(__name__)


class CatalogError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail
        super().__init__(detail)


class CatalogConflict(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


class CatalogReferenceInvalid(InvalidRequestError, CatalogValidationError):
    """The requested candidate names a Model that is not an active input."""

    def __init__(self, code: CatalogCode, detail: str) -> None:
        CatalogValidationError.__init__(self, code, detail)
        self.typed_reason = InvalidRequestReason.NOT_FOUND
        self.typed_field = "models"


class CatalogEntityService:
    """Store immutable Model/Recipe revisions and switch active heads."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        cursors: CursorCodec | None = None,
    ) -> None:
        if isinstance(sessions, Session):
            self._session: Session | None = sessions
            self._sessions: sessionmaker[Session] | None = None
        else:
            self._session = None
            self._sessions = sessions
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

    def refresh_build_policy(self) -> None:
        """Recompile derived platform policy without rewriting Recipe revisions.

        As with catalog publication metadata, only the typed projection is
        updated. Documents, content digests, heads, and already dispatched
        build requests retain their immutable identities.

        This runs at the start of every sync over every active revision, so it
        is also where a projection written under an earlier contract is
        re-derived from its immutable document: the sync leaves an unchanged
        head alone and never visits a superseded revision.
        """
        with self._write() as session:
            revisions = session.scalars(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
                .with_for_update()
            ).all()
            heads = set(
                session.scalars(
                    select(CatalogDocumentHead.active_revision_id).where(
                        CatalogDocumentHead.kind == "recipe"
                    )
                )
            )
            for revision in revisions:
                try:
                    recipe = read_catalog_document(revision)
                except CatalogRevisionContractError as error:
                    # Written under an earlier contract. Nothing can read the
                    # document, so there is nothing to derive a projection
                    # from; a superseded revision is simply history. One such
                    # row must not stop the rest of the catalog.
                    _LOGGER.log(
                        logging.WARNING if revision.id in heads else logging.DEBUG,
                        "skipping build policy refresh for revision %s: %s",
                        revision.id,
                        error,
                    )
                    session.expunge(revision)
                    continue
                assert isinstance(recipe, RecipeDefinition)
                try:
                    projected = RecipeRevisionProjection.model_validate_json(
                        canonical_message(revision.projected)
                    ).model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                    healed = False
                except (CatalogRevisionContractError, TypeError, ValueError) as error:
                    # The document is readable but the stored projection is
                    # not (it predates the current projection contract).
                    try:
                        projected = _rederive_projection(session, revision, recipe)
                    except CatalogRevisionContractError as failure:
                        _LOGGER.warning(
                            "skipping build policy refresh for revision %s: %s; "
                            "it cannot be re-derived: %s",
                            revision.id,
                            error,
                            failure,
                        )
                        continue
                    healed = True
                    _LOGGER.info(
                        "re-derived the catalog projection of revision %s from "
                        "its document",
                        revision.id,
                    )
                policy = build_policy_projection(recipe)
                if not healed and all(
                    projected.get(key) == value for key, value in policy.items()
                ):
                    continue
                projected.update(policy)
                session.execute(
                    update(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.id == revision.id,
                    )
                    .values(
                        projected=write_catalog_projection(projected, kind="recipe")
                    )
                )
                session.expire(revision, ["projected"])

    def record_prebuilt_images(
        self, images: Mapping[tuple[str, str, str], PrebuiltImage | None]
    ) -> None:
        """Record the signed catalog's prebuilt image on each active recipe revision.

        ``images`` is keyed by ``(publisher, slug, content digest)``: a
        prebuilt image belongs to one exact revision. The signed index is the
        only source, so a revision it no longer pins loses its image and
        builds on a Spark instead.
        """
        with self._write() as session:
            revisions = session.scalars(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
                .with_for_update()
            ).all()
            for revision in revisions:
                try:
                    projected = read_catalog_projection(revision)
                except CatalogRevisionContractError:
                    session.expunge(revision)
                    continue
                if not isinstance(projected, RecipeRevisionProjection):
                    continue
                image = images.get(
                    (revision.publisher, revision.slug, revision.content_digest or "")
                )
                if projected.prebuilt_image == image:
                    continue
                session.execute(
                    update(CatalogDocumentRevision)
                    .where(CatalogDocumentRevision.id == revision.id)
                    .values(
                        projected=write_catalog_projection(
                            projected.model_copy(update={"prebuilt_image": image})
                        )
                    )
                )
                session.expire(revision, ["projected"])

    def create_draft(
        self, document: Mapping[str, object], *, actor: str
    ) -> CatalogDocumentRevision:
        parsed, clean, kind, publisher, slug, title = _parse(document)
        now, actor = self._clock(), _actor(actor)
        try:
            with self._write() as session:
                root = CatalogDocument(
                    kind=kind,
                    publisher=publisher,
                    slug=slug,
                    title=title,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(root)
                session.flush()
                revision = _revision(root, parsed, clean, 1, actor, now)
                session.add(revision)
                session.flush()
                session.add(
                    CatalogDocumentHead(
                        kind=kind,
                        publisher=publisher,
                        slug=slug,
                        candidate_revision_id=revision.id,
                        generation=0,
                    )
                )
                session.flush()
                return revision
        except IntegrityError as error:
            raise CatalogConflict(
                CatalogCode.DOCUMENT_EXISTS, "catalog document identity already exists"
            ) from error

    def revise(
        self,
        document_id: str,
        document: Mapping[str, object],
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> CatalogDocumentRevision:
        parsed, clean, kind, publisher, slug, title = _parse(document)
        now, actor = self._clock(), _actor(actor)
        with self._write() as session:
            root = session.scalar(
                select(CatalogDocument)
                .where(CatalogDocument.id == document_id)
                .with_for_update()
            )
            if root is None:
                raise KeyError(document_id)
            if (root.kind, root.publisher, root.slug) != (kind, publisher, slug):
                raise CatalogValidationError(
                    CatalogCode.IDENTITY_CHANGED, "document identity cannot change"
                )
            head = _head(session, root)
            latest = session.scalar(
                select(CatalogDocumentRevision.revision_number)
                .where(CatalogDocumentRevision.document_id == root.id)
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            )
            # An empty root has no accepted revision to protect. The new
            # request supplies the canonical document instead of waiting for
            # bookkeeping that cannot repair itself.
            latest_number = latest if latest is not None else 0
            if expected_revision is not None and latest_number != expected_revision:
                raise CatalogConflict(
                    CatalogCode.STALE_REVISION, "document revision changed"
                )
            revision = _revision(root, parsed, clean, latest_number + 1, actor, now)
            session.add(revision)
            session.flush()
            head.candidate_revision_id, root.title, root.updated_at = (
                revision.id,
                title,
                now,
            )
            session.flush()
            return revision

    def resolve(
        self,
        entity_or_revision_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> CatalogDocumentRevision:
        del actor
        with self._write() as session:
            revision = session.get(CatalogDocumentRevision, entity_or_revision_id)
            root = (
                session.get(CatalogDocument, revision.document_id, with_for_update=True)
                if revision
                else session.get(
                    CatalogDocument, entity_or_revision_id, with_for_update=True
                )
            )
            if root is None:
                raise KeyError(entity_or_revision_id)
            head = _head(session, root)
            if revision is None:
                revision = (
                    session.get(CatalogDocumentRevision, head.candidate_revision_id)
                    if head.candidate_revision_id
                    else None
                )
            if revision is None:
                raise KeyError(entity_or_revision_id)
            if revision.state == "active":
                if (
                    head.active_revision_id is None
                    and head.candidate_revision_id is None
                ):
                    # The caller named this exact activation; history did not
                    # select it. Existing selections and candidates still win.
                    head.active_revision_id = revision.id
                    head.generation += 1
                    session.flush()
                return revision
            if revision.id != head.candidate_revision_id:
                raise CatalogConflict(
                    CatalogCode.NOT_CANDIDATE,
                    "only the current candidate can be activated",
                )
            if (
                expected_revision is not None
                and revision.revision_number != expected_revision
            ):
                raise CatalogConflict(
                    CatalogCode.STALE_REVISION, "document revision changed"
                )
            if revision.kind == "recipe":
                self._bind_recipe_models(session, revision)
            revision.state, head.active_revision_id, head.candidate_revision_id = (
                "active",
                revision.id,
                None,
            )
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
                    try:
                        projected = read_catalog_projection(candidate).model_dump(
                            mode="json", exclude_none=False
                        )
                    except CatalogRevisionContractError:
                        # Optional history annotation cannot retain the old gate.
                        projected = None
                    if projected is not None:
                        projected["failure_reason"] = reason[:240]
                        candidate.projected = write_catalog_projection(
                            projected, kind=candidate.kind
                        )
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
            revision = session.get(
                CatalogDocumentRevision,
                head.active_revision_id or head.candidate_revision_id,
            )
            if revision is None:
                raise KeyError(entity_id)
            return revision

    def resolve_reference(self, reference: object) -> CatalogDocumentRevision:
        kind = getattr(
            getattr(reference, "kind", None), "value", getattr(reference, "kind", None)
        )
        publisher, slug, digest = (
            getattr(reference, key, None)
            for key in ("publisher", "slug", "content_sha256")
        )
        if kind not in {"model", "recipe"} or not all(
            isinstance(value, str) for value in (publisher, slug, digest)
        ):
            raise CatalogValidationError(
                CatalogCode.REFERENCE,
                "only exact model/recipe references are supported",
            )
        with self._read() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == kind,
                    CatalogDocumentRevision.publisher == publisher,
                    CatalogDocumentRevision.slug == slug,
                    CatalogDocumentRevision.content_digest == digest,
                    CatalogDocumentRevision.state == "active",
                )
                .limit(1)
            )
            if revision is None:
                raise CatalogValidationError(
                    CatalogCode.REFERENCE_MISSING,
                    "exact referenced document is not active",
                )
            return revision

    def _bind_recipe_models(
        self, session: Session, revision: CatalogDocumentRevision
    ) -> None:
        recipe = read_catalog_document(revision)
        if not isinstance(recipe, RecipeDefinition):
            raise CatalogValidationError(
                CatalogCode.RECIPE_INVALID, "recipe revision is not a recipe document"
            )
        models: dict[str, ModelDefinition] = {}
        bindings = []
        artifact_inputs = []
        for selection in recipe.models:
            ref = selection.model
            model_revision = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.publisher == ref.publisher,
                    CatalogDocumentRevision.slug == ref.slug,
                    CatalogDocumentRevision.content_digest == ref.content_sha256,
                    CatalogDocumentRevision.state == "active",
                )
                .limit(1)
            )
            if model_revision is None:
                raise CatalogReferenceInvalid(
                    CatalogCode.MODEL_REFERENCE_MISSING,
                    f"model reference is missing: {ref.publisher}/{ref.slug}",
                )
            model = read_catalog_document(model_revision)
            if not isinstance(model, ModelDefinition):
                raise CatalogValidationError(
                    CatalogCode.MODEL_REFERENCE_INVALID,
                    "referenced revision is not a model document",
                )
            models[model_revision.content_digest] = model
            if model_revision.artifact_key is None:
                # The immutable Model owns the files; this key is disposable
                # derived bookkeeping, reconstructed from the exact document.
                artifact_key = _digest(
                    {
                        "files": _model_artifact_files(model),
                        "format": model.format.model_dump(mode="json"),
                    }
                )
                session.execute(
                    update(CatalogDocumentRevision)
                    .where(CatalogDocumentRevision.id == model_revision.id)
                    .values(artifact_key=artifact_key)
                )
                session.expire(model_revision, ["artifact_key"])
            artifact_inputs.append(
                {
                    "selection_id": selection.id,
                    "artifact_key": model_revision.artifact_key,
                }
            )
            bindings.append(
                CatalogRecipeModelReference(
                    recipe_revision_id=revision.id,
                    recipe_kind="recipe",
                    selection_id=selection.id,
                    model_revision_id=model_revision.id,
                    model_kind="model",
                    model_publisher=ref.publisher,
                    model_slug=ref.slug,
                    model_content_digest=ref.content_sha256,
                )
            )
        try:
            validate_recipe_models(recipe, models)
        except ValueError as error:
            raise CatalogValidationError(
                CatalogCode.MODEL_REFERENCE_INVALID, str(error)
            ) from error
        revision.artifact_key = _digest({"models": artifact_inputs})
        revision.execution_key = _digest(
            {
                "execution": _execution_projection(recipe),
                "artifact_key": revision.artifact_key,
            }
        )
        projected = read_catalog_projection(revision).model_dump(
            mode="json", exclude_none=False
        )
        projected["artifact_inputs"] = artifact_inputs
        revision.projected = write_catalog_projection(projected, kind=revision.kind)
        session.add_all(bindings)


def _parse(
    document: Mapping[str, object],
) -> tuple[ModelDefinition | RecipeDefinition, dict[str, object], str, str, str, str]:
    try:
        # Keep the published document exactly as received: its digest is the
        # document identity recipes reference.
        clean = json.loads(json.dumps(dict(document), allow_nan=False))
        parsed = (
            read_model(clean) if clean.get("kind") == "model" else read_recipe(clean)
        )
    except Exception as error:
        raise CatalogValidationError(
            CatalogCode.DOCUMENT_INVALID,
            "document does not satisfy the public recipe contract",
        ) from error
    title = (
        parsed.identity.model.title
        if isinstance(parsed, ModelDefinition)
        else parsed.metadata.title
    )
    return (
        parsed,
        clean,
        str(parsed.kind),
        parsed.identity.publisher,
        parsed.identity.slug,
        title,
    )


def _revision(
    root: CatalogDocument,
    parsed: ModelDefinition | RecipeDefinition,
    clean: dict[str, object],
    number: int,
    actor: str,
    now: datetime,
) -> CatalogDocumentRevision:
    artifact_key = download = installed = None
    if isinstance(parsed, ModelDefinition):
        files = _model_artifact_files(parsed)
        artifact_key = _digest(
            {"files": files, "format": parsed.format.model_dump(mode="json")}
        )
        download, installed = parsed.download_bytes, parsed.installed_bytes
        projected = {
            "identity": parsed.identity.model_dump(mode="json"),
            "modalities": parsed.modalities,
            "artifact_count": len(parsed.files),
            "download_bytes": download,
            "installed_bytes": installed,
        }
    else:
        projected = recipe_document_projection(parsed)
    return CatalogDocumentRevision(
        document_id=root.id,
        kind=str(parsed.kind),
        publisher=parsed.identity.publisher,
        slug=parsed.identity.slug,
        revision_number=number,
        schema_version=2,
        state="candidate",
        document=copy.deepcopy(clean),
        content_digest=document_sha256(clean),
        artifact_key=artifact_key,
        execution_key=_digest(_execution_projection(parsed)),
        download_bytes=download,
        installed_bytes=installed,
        projected=write_catalog_projection(projected, kind=str(parsed.kind)),
        created_by=actor,
        created_at=now,
    )


def recipe_document_projection(recipe: RecipeDefinition) -> dict[str, object]:
    """Every projection field the immutable recipe document alone determines."""
    return {
        "title": recipe.metadata.title,
        "description": recipe.metadata.description,
        "tags": recipe.metadata.tags,
        "runtime_engine": recipe.runtime.engine,
        "topology": recipe.topology.model_dump(mode="json"),
        **build_policy_projection(recipe),
    }


# Fields the catalog sync records beside the document; the document cannot
# supply them, so a re-derived projection keeps each one that is still valid.
_SYNC_RECORDED_FIELDS = (
    "publication_commit",
    "source_path",
    "package_sha256",
    "source_bundle_sha256",
    "package_handle",
    "release_version",
    "release_released_at",
    "prebuilt_image",
)


def _rederive_projection(
    session: Session,
    revision: CatalogDocumentRevision,
    recipe: RecipeDefinition,
) -> dict[str, object]:
    """Rebuild a recipe projection that no longer validates from its document.

    Only a document that parsed and still matches its stored content digest is
    trusted; anything else is corruption and is left for the caller to report
    rather than repaired into a valid-looking projection.
    """
    projected = recipe_document_projection(recipe)
    stored = revision.projected if isinstance(revision.projected, Mapping) else {}
    for key in _SYNC_RECORDED_FIELDS:
        if stored.get(key) is None:
            continue
        try:
            write_catalog_projection(
                {**projected, key: stored[key]},
                kind="recipe",
            )
        except CatalogRevisionContractError:
            continue  # the next sync records it again from the signed catalog
        projected[key] = stored[key]
    selections = {selection.id for selection in recipe.models}
    bindings = {
        row.selection_id: artifact_key
        for row, artifact_key in session.execute(
            select(CatalogRecipeModelReference, CatalogDocumentRevision.artifact_key)
            .join(
                CatalogDocumentRevision,
                CatalogDocumentRevision.id
                == CatalogRecipeModelReference.model_revision_id,
            )
            .where(CatalogRecipeModelReference.recipe_revision_id == revision.id)
        )
    }
    if selections <= bindings.keys() and all(bindings[i] for i in selections):
        projected["artifact_inputs"] = [
            {"selection_id": selection.id, "artifact_key": bindings[selection.id]}
            for selection in recipe.models
        ]
    write_catalog_projection(projected, kind="recipe")  # still invalid: corrupt
    return projected


def build_policy_projection(recipe: RecipeDefinition) -> dict[str, object]:
    """Compile platform-owned rootless build policy for every source recipe.

    These are admission budgets, not measured image sizes. The builder still
    measures and verifies the exported archive before it can be distributed.
    No public Recipe fields or local author overrides grant build authority.
    """
    gib = 1024**3
    disks = [role.resources.disk for role in recipe.topology.roles]
    image_bytes = max(disk.image_bytes for disk in disks)
    return {
        "build_resources": {
            "cpu_cores": 8,
            "download_bytes": image_bytes,
            "temporary_bytes": min(
                16 * 1024**4,
                max(3 * image_bytes, *(disk.working_bytes for disk in disks)),
            ),
            "memory_bytes": min(64 * gib, max(2 * gib, image_bytes)),
            "processes": 4096,
            "timeout_seconds": 86_400,
        },
        # Installation/ownership operations inside the existing rootless
        # namespace only. GPU, host mounts, sockets and privilege stay off.
        "build_security": {
            "capabilities": [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
                "SETFCAP",
                "SETGID",
                "SETUID",
            ],
        },
        "build_options": {
            "additional_contexts": [],
            "annotations": [],
            "environment": [],
            "format": "oci",
            "identity_label": True,
            "ignorefile": None,
            "jobs": 1,
            "labels": [],
            "layer_compression": "disabled",
            "layer_labels": [],
            "layers": False,
            "no_hostname": False,
            "no_hosts": False,
            "omit_history": False,
            "os_features": [],
            "os_version": None,
            "shm_bytes": 64 * 1024**2,
            "skip_unused_stages": True,
            "squash": "none",
            "timestamp": None,
            "unset_environment": [],
            "unset_labels": [],
        },
    }


def _model_artifact_files(parsed: ModelDefinition) -> list[dict[str, object]]:
    """The model's files as installed bytes.

    A source that ships a file split into parts is a transport detail: the same
    bytes are the same artifact, so the parts never enter an artifact key.
    """

    return [item.model_dump(mode="json", exclude={"parts"}) for item in parsed.files]


def _execution_projection(parsed: ModelDefinition | RecipeDefinition) -> object:
    if isinstance(parsed, ModelDefinition):
        return {
            "artifact_key": _digest(
                {
                    "files": _model_artifact_files(parsed),
                    "format": parsed.format.model_dump(mode="json"),
                }
            )
        }
    return {
        "execution": parsed.execution.model_dump(mode="json"),
        "runtime": parsed.runtime.model_dump(mode="json"),
        "topology": parsed.topology.model_dump(mode="json"),
        "interfaces": [item.model_dump(mode="json") for item in parsed.interfaces],
        "settings": parsed.settings.model_dump(mode="json"),
    }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _head(session: Session, root: CatalogDocument) -> CatalogDocumentHead:
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
        # Every caller holds this document root's write lock. Recreate only its
        # empty selection record: history cannot choose an accepted head. The
        # current authorized request binds the candidate or imported revision.
        head = CatalogDocumentHead(
            kind=root.kind,
            publisher=root.publisher,
            slug=root.slug,
            generation=0,
        )
        session.add(head)
        session.flush()
    return head


def _actor(actor: str) -> str:
    return actor if isinstance(actor, str) and actor else "system"


__all__ = [
    "CatalogConflict",
    "CatalogDocument",
    "CatalogDocumentHead",
    "CatalogDocumentRevision",
    "CatalogEntityService",
    "CatalogError",
    "CatalogRecipeModelReference",
    "CatalogValidationError",
]
