"""Catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode, canonical_message
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from ..catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
)
from ..categorized_errors import InvalidType
from ..models import CatalogDocumentRevision
from .artifacts import (
    ArtifactPart,
    ArtifactSpec,
    _is_hex,
    _validate_artifact,
)
from .catalog_helpers import _source_for_catalog_artifact
from .errors import ModelCacheResolutionInvalid, ModelCacheResolutionRefused
from .input_contracts import CatalogArtifact, FixtureArtifact

if TYPE_CHECKING:
    from .service import ModelCacheService


class CatalogMixin:
    """Catalog behavior of the cache service."""

    @staticmethod
    def _newest_readable_revision(
        session: Session, row: CatalogDocumentRevision
    ) -> CatalogDocumentRevision | None:
        """Newest active revision of the same document this Controller can read.

        A revision written under another contract, or since replaced, is not
        usable but must not stop a manifest: the same recipe or model is
        resolved from its newest readable revision instead.
        """

        for candidate in session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == row.kind,
                CatalogDocumentRevision.document_id == row.document_id,
                CatalogDocumentRevision.state == "active",
            )
            .order_by(
                CatalogDocumentRevision.revision_number.desc(),
                CatalogDocumentRevision.created_at.desc(),
                CatalogDocumentRevision.id.desc(),
            )
            .limit(16)
        ):
            try:
                read_catalog_document(candidate)
            except CatalogRevisionContractError:
                continue
            return candidate
        return None

    def _recipe_document(
        self,
        session: Session,
        digest: str | None,
        revision_id: str | None,
        *,
        tolerant: bool = False,
    ) -> tuple[RecipeDefinition, str, str]:
        cache = cast("ModelCacheService", self)
        if revision_id is not None:
            revision = session.get(CatalogDocumentRevision, revision_id)
            if tolerant and revision is not None and revision.kind == "recipe":
                readable = revision.state == "active"
                if readable:
                    try:
                        read_catalog_document(revision)
                    except CatalogRevisionContractError:
                        readable = False
                if not readable:
                    revision = cache._newest_readable_revision(session, revision)
        else:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.content_digest == digest,
                    CatalogDocumentRevision.state == "active",
                )
            )
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or not isinstance(revision.content_digest, str)
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_REVISION_MISSING,
                "exact recipe revision is not resolved",
            )
        try:
            recipe = read_catalog_document(revision)
            if not isinstance(recipe, RecipeDefinition):
                raise InvalidType("catalog revision is not a recipe")
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_INVALID, "canonical recipe definition is invalid"
            ) from error
        return recipe, revision.id, revision.content_digest

    def _collect_model_definitions(
        self,
        session: Session,
        digest: str,
        rows: dict[str, CatalogDocumentRevision],
        *,
        visiting: set[str] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        if not isinstance(digest, str) or not _is_hex(digest) or len(digest) != 64:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_PIN_INVALID, "model dependency pin is invalid"
            )
        if digest in rows:
            if visiting is not None and digest in visiting:
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.MODEL_DEPENDENCY_CYCLE,
                    "canonical model dependency graph contains a cycle",
                )
            return
        active = visiting if visiting is not None else set()
        if digest in active:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEPENDENCY_CYCLE,
                "canonical model dependency graph contains a cycle",
            )
        active.add(digest)
        row = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest == digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if row is None:
            active.remove(digest)
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEFINITION_MISSING,
                "exact model definition is not resolved",
            )
        if aliases is not None:
            try:
                read_catalog_document(row)
            except CatalogRevisionContractError:
                # Tolerant resolution: the same model's newest readable
                # revision stands in, keyed by its own digest.
                substitute = cache._newest_readable_revision(session, row)
                if substitute is not None and isinstance(
                    substitute.content_digest, str
                ):
                    aliases[digest] = substitute.content_digest
                    active.remove(digest)
                    cache._collect_model_definitions(
                        session,
                        substitute.content_digest,
                        rows,
                        visiting=active,
                        aliases=aliases,
                    )
                    return
        try:
            definition = read_catalog_document(row)
            if not isinstance(definition, ModelDefinition):
                raise InvalidType("catalog revision is not a model")
            rows[digest] = row
            for dependency in definition.dependencies:
                cache._collect_model_definitions(
                    session,
                    dependency.content_sha256,
                    rows,
                    visiting=active,
                    aliases=aliases,
                )
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEFINITION_INVALID,
                "canonical model definition is invalid",
            ) from error
        finally:
            active.remove(digest)

    def _artifact_from_catalog(
        self,
        value: CatalogArtifact,
        *,
        model_content_sha256: str,
    ) -> ArtifactSpec:
        cache = cast("ModelCacheService", self)
        raw_id = value.id
        raw_path = value.path
        raw_kind = value.kind
        raw_digest = value.sha256
        raw_bytes = value.download_bytes
        roles = value.roles
        if (
            not isinstance(raw_id, str)
            or not isinstance(raw_path, str)
            or not isinstance(raw_kind, str)
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID,
                "catalog artifact identity is incomplete",
            )
        if (
            not isinstance(raw_digest, str)
            or not isinstance(raw_bytes, int)
            or not isinstance(roles, list)
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID,
                "catalog artifact integrity metadata is incomplete",
            )
        if (
            raw_kind not in {"huggingface.file", "github-release.asset"}
            and not cache._fixture_sources
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_UNTRUSTED,
                "production cache downloads require a trusted catalog artifact reference",
            )
        source, revision = _source_for_catalog_artifact(value)
        parts = cache._parts_from_catalog(value)
        spec = ArtifactSpec(
            # The file digest/path is the reusable identity.  A model
            # revision digest is retained as provenance below only.
            key=f"artifact-{raw_digest[:12]}-{raw_id}",
            artifact_id=raw_id,
            path=raw_path,
            kind=raw_kind,
            repository=(None if value.repository is None else str(value.repository)),
            source=source,
            revision=revision,
            sha256=raw_digest,
            expected_bytes=raw_bytes,
            roles=tuple(str(role) for role in roles),
            model_content_sha256=model_content_sha256,
            parts=parts,
        )
        _validate_artifact(spec)
        return spec

    @staticmethod
    def _parts_from_catalog(
        value: CatalogArtifact,
    ) -> tuple[ArtifactPart, ...] | None:
        """The split parts a catalog file declares, each with its own source URL."""

        if value.parts is None:
            return None
        return tuple(
            ArtifactPart(
                path=part.path,
                source=_source_for_catalog_artifact(
                    value.model_copy(update={"path": part.path})
                )[0],
                sha256=part.sha256,
                expected_bytes=part.download_bytes,
            )
            for part in value.parts
        )

    def _artifact_from_input(
        self,
        value: object,
        *,
        model_content_sha256: str | None,
    ) -> ArtifactSpec:
        try:
            artifact = (
                value
                if isinstance(value, FixtureArtifact)
                else FixtureArtifact.model_validate_json(canonical_message(value))
            )
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.ARTIFACT_INVALID, "cache artifact input is invalid"
            ) from error
        artifact_id = artifact.id
        path = artifact.path
        kind = artifact.kind
        repository = artifact.repository
        source = artifact.source or (
            repository if kind in {"http.file", "file"} else None
        )
        assert source is not None  # the ingress contract requires a source
        revision = artifact.revision
        digest = artifact.sha256
        expected_bytes = artifact.download_bytes
        raw_roles = artifact.roles
        parts = (
            None
            if artifact.parts is None
            else tuple(
                ArtifactPart(
                    path=part.path,
                    source=part.source,
                    sha256=part.sha256,
                    expected_bytes=part.download_bytes,
                )
                for part in artifact.parts
            )
        )
        spec = ArtifactSpec(
            key=(
                f"artifact-{model_content_sha256[:12]}-{artifact_id}"
                if model_content_sha256 is not None
                else f"artifact-input-{artifact_id}"
            ),
            artifact_id=artifact_id,
            path=path,
            kind=kind,
            repository=(None if repository is None else str(repository)),
            source=source,
            revision=revision,
            sha256=digest,
            expected_bytes=expected_bytes,
            roles=tuple(str(role) for role in raw_roles),
            model_content_sha256=model_content_sha256,
            parts=parts,
        )
        _validate_artifact(spec)
        return spec
