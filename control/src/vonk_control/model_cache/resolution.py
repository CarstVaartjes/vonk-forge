"""Resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheBlockerCode, ModelCacheCode
from vonk_forge_contracts import ModelDefinition, RecipeDefinition
from vonk_forge_contracts.model import ModelReference

from ..bounded_retry import bounded_attempts
from ..catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
)
from ..model_cache_contract import (
    UUID_PATTERN,
    CachedModelResolution,
    CachedRecipeResolution,
    CachedResourceEstimate,
    CacheResolution,
)
from ..models import CatalogDocumentRevision, ModelCacheSet
from ..revision_images import RevisionImage, revision_images
from .artifacts import (
    ArtifactSetManifest,
    ArtifactSpec,
    _composed_artifacts,
    _is_digest,
    _optional_digest,
    _unique_artifacts,
    _validate_manifest,
)
from .catalog_helpers import (
    _canonical_model_artifacts,
    _recipe_model_content_digests,
    _recipe_model_file_ids,
)
from .constants import _DIGEST_PATTERN
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheError,
    ModelCacheResolutionInvalid,
    ModelCacheResolutionRefused,
    ModelCacheStorageUnknown,
)
from .source_helpers import _model_selector

if TYPE_CHECKING:
    from .service import ModelCacheService


class ResolutionMixin:
    """Resolution behavior of the cache service."""

    def resolve_artifact_set(
        self,
        *,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[object] | None = None,
    ) -> ArtifactSetManifest:
        cache = cast("ModelCacheService", self)
        last_unknown: ModelCacheStorageUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return cache._resolve_artifact_set_once(
                    model_content_sha256=model_content_sha256,
                    recipe_revision_sha256=recipe_revision_sha256,
                    recipe_revision_id=recipe_revision_id,
                    artifacts=artifacts,
                )
            except ModelCacheStorageUnknown as error:
                last_unknown = error
        if last_unknown is not None:
            raise last_unknown
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE,
            "exact catalog observation is unavailable",
        )

    def _resolve_artifact_set_once(
        self,
        *,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[object] | None = None,
    ) -> ArtifactSetManifest:
        cache = cast("ModelCacheService", self)
        model_digest = _optional_digest(model_content_sha256)
        recipe_digest = _optional_digest(recipe_revision_sha256)
        if recipe_digest is not None and recipe_revision_id is not None:
            raise ModelCacheResolutionRefused(
                ModelCacheCode.RECIPE_IDENTITY_AMBIGUOUS,
                "recipe revision digest and ID cannot both be supplied",
            )
        if artifacts is not None:
            if not cache._fixture_sources:
                raise ModelCacheResolutionRefused(
                    ModelCacheCode.FIXTURE_SOURCES_FORBIDDEN,
                    "caller-supplied artifact sources are only available to fixture services",
                )
            provided_specs = tuple(
                cache._artifact_from_input(value, model_content_sha256=model_digest)
                for value in artifacts
            )
            manifest = ArtifactSetManifest(
                model_content_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_content_digests=(() if model_digest is None else (model_digest,)),
                artifacts=tuple(sorted(provided_specs, key=lambda item: item.key)),
            )
            _validate_manifest(manifest)
            return manifest

        if (
            model_digest is None
            and recipe_digest is None
            and recipe_revision_id is None
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.PIN_REQUIRED,
                "an exact model definition or recipe revision is required",
            )
        with cache._session() as session:
            recipe_document: RecipeDefinition | None = None
            recipe_model_digests: list[str] = []
            if recipe_digest is not None or recipe_revision_id is not None:
                recipe_document, _resolved_recipe_id, resolved_recipe_digest = (
                    cache._recipe_document(session, recipe_digest, recipe_revision_id)
                )
                if (
                    recipe_digest is not None
                    and resolved_recipe_digest != recipe_digest
                ):
                    raise ModelCacheStorageUnknown(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
                        "exact recipe revision is not resolved",
                    )
                recipe_digest = resolved_recipe_digest
                recipe_model_digests = _recipe_model_content_digests(recipe_document)
                if not recipe_model_digests:
                    raise ModelCacheStorageUnknown(
                        ModelCacheCode.RECIPE_MODEL_MISSING,
                        "recipe does not bind an exact model definition",
                    )
                if (
                    model_digest is not None
                    and model_digest not in recipe_model_digests
                ):
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.PIN_MISMATCH,
                        "recipe and requested model definitions do not match",
                    )
                model_digest = model_digest or recipe_model_digests[0]
            if model_digest is None:
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.PIN_REQUIRED,
                    "an exact model definition is required after recipe resolution",
                )
            model_rows: dict[str, CatalogDocumentRevision] = {}
            requested_model_digests = (
                recipe_model_digests if recipe_document is not None else [model_digest]
            )
            for digest in requested_model_digests:
                cache._collect_model_definitions(session, digest, model_rows)
            specs: list[ArtifactSpec] = []
            model_ref: ModelReference | None = None
            for digest, row in sorted(model_rows.items()):
                if digest == model_digest:
                    model_ref = ModelReference(
                        publisher=row.publisher,
                        slug=row.slug,
                        content_sha256=digest,
                    )
                raw_artifacts = _canonical_model_artifacts(row)
                selected_ids = _recipe_model_file_ids(recipe_document, digest)
                for raw in raw_artifacts:
                    if selected_ids is not None and raw.id not in selected_ids:
                        continue
                    specs.append(
                        cache._artifact_from_catalog(
                            raw,
                            model_content_sha256=digest,
                        )
                    )
            manifest = ArtifactSetManifest(
                model_content_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_content_digests=tuple(sorted(model_rows)),
                artifacts=tuple(
                    sorted(_composed_artifacts(specs), key=lambda item: item.key)
                ),
                model_definition_ref=model_ref,
            )
        _validate_manifest(manifest)
        return manifest

    def _resolve_model_selector(self, selector: str) -> str:
        """Resolve one operator selector to an active model content digest.

        The projection service owns the human-facing list/detail response;
        this mutation boundary still resolves the same canonical identities so
        a request cannot evict a guessed or ambiguous cache entry.  Digests,
        catalog UUIDs, ``publisher/slug`` and an exact slug are accepted.
        """
        cache = cast("ModelCacheService", self)

        selector = _model_selector(selector).casefold()
        last_unknown: ModelCacheStorageUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                with cache._session() as session:
                    return cache._resolve_model_selector_in_session(session, selector)
            except ModelCacheStorageUnknown as error:
                last_unknown = error
        if last_unknown is not None:
            raise last_unknown
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE,
            "exact catalog observation is unavailable",
        )

    @staticmethod
    def _resolve_model_selector_in_session(session: Session, selector: str) -> str:
        """Resolve one current model selector inside its caller's snapshot."""

        if re.fullmatch(_DIGEST_PATTERN, selector):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "model",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest == selector,
                    )
                )
            )
            if not rows:
                cached_digests = list(
                    session.scalars(
                        select(ModelCacheSet.model_content_sha256).where(
                            ModelCacheSet.model_content_sha256 == selector
                        )
                    )
                )
                if cached_digests:
                    return selector
            if rows:
                return selector
        elif re.fullmatch(UUID_PATTERN, selector):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "model",
                        CatalogDocumentRevision.state == "active",
                        (CatalogDocumentRevision.id == selector)
                        | (CatalogDocumentRevision.document_id == selector),
                    )
                )
            )
        else:
            publisher = slug = None
            if "/" in selector:
                publisher, slug = selector.split("/", 1)
            conditions = [
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            ]
            if publisher is not None:
                conditions.append(CatalogDocumentRevision.publisher == publisher)
                conditions.append(CatalogDocumentRevision.slug == slug)
            else:
                conditions.append(CatalogDocumentRevision.slug == selector)
            rows = list(
                session.scalars(select(CatalogDocumentRevision).where(*conditions))
            )
        # A catalog row whose identity digest is damaged cannot be pinned: it is
        # skipped like any row the catalog does not hold, and the rest carry on.
        rows = [row for row in rows if _is_digest(row.content_digest)]
        if len(rows) != 1:
            if not rows:
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SELECTOR_MISSING, "model selector was not found"
                )
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SELECTOR_AMBIGUOUS,
                "model selector matches multiple models",
            )
        return str(rows[0].content_digest)

    def resolve_latest_cached(
        self,
        *,
        recipe_identity: str,
        model_content_sha256: str | None = None,
        model_variant: str | None = None,
        exact_revision_id: str | None = None,
    ) -> CacheResolution:
        """Resolve one logical Recipe to its newest usable cached revision.

        The profile read/load path calls this method for both web and CLI
        operators.  It is read-only: missing content is reported, never
        downloaded.  A newer active revision may be uncached; in that case we
        return the newest compatible cached receipt and mark that a catalog
        update is available.  If no revision is cached, the newest active
        revision is returned with unknown (``None``) resource estimates so a
        load operation can prepare it explicitly.

        An explicitly selected exact revision is never replaced by an older
        cached one: either the caller names it in ``exact_revision_id``, or
        ``recipe_identity`` is itself a revision id or content digest.  The
        exact revision is returned with ``cached=False`` and a
        ``recipe-not-cached`` blocker when its authorized archive is absent, so
        the operator sees the missing asset instead of a silent substitution.
        """
        cache = cast("ModelCacheService", self)
        last_unknown: ModelCacheStorageUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return cache._resolve_latest_cached_once(
                    recipe_identity=recipe_identity,
                    model_content_sha256=model_content_sha256,
                    model_variant=model_variant,
                    exact_revision_id=exact_revision_id,
                )
            except ModelCacheStorageUnknown as error:
                last_unknown = error
        if last_unknown is not None:
            raise last_unknown
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE,
            "exact catalog observation is unavailable",
        )

    def _resolve_latest_cached_once(
        self,
        *,
        recipe_identity: str,
        model_content_sha256: str | None = None,
        model_variant: str | None = None,
        exact_revision_id: str | None = None,
    ) -> CacheResolution:
        cache = cast("ModelCacheService", self)

        if (
            not isinstance(recipe_identity, str)
            or not 1 <= len(recipe_identity.strip()) <= 256
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_IDENTITY_INVALID, "recipe identity is required"
            )
        identity = recipe_identity.strip().casefold()
        requested_model = _optional_digest(model_content_sha256)
        if model_variant is not None and (
            not isinstance(model_variant, str)
            or not 1 <= len(model_variant.strip()) <= 128
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_VARIANT_INVALID, "model variant is invalid"
            )
        requested_variant = (
            model_variant.strip() if isinstance(model_variant, str) else None
        )

        with cache._session() as session:
            if re.fullmatch(_DIGEST_PATTERN, identity):
                seed = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.content_digest == identity,
                    )
                )
            elif re.fullmatch(UUID_PATTERN, identity):
                seed = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        (CatalogDocumentRevision.id == identity)
                        | (CatalogDocumentRevision.document_id == identity),
                    )
                )
            elif "/" in identity:
                publisher, slug = identity.split("/", 1)
                seed = session.scalar(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.publisher == publisher,
                        CatalogDocumentRevision.slug == slug,
                    )
                    .order_by(CatalogDocumentRevision.revision_number.desc())
                )
            else:
                seed = session.scalar(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.slug == identity,
                    )
                    .order_by(CatalogDocumentRevision.revision_number.desc())
                )
            if seed is None:
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.RECIPE_IDENTITY_MISSING,
                    "recipe identity was not found",
                )

            # An explicitly selected exact revision must never be silently
            # replaced by a newer one and must never fall back to an older
            # cached one.
            exact: CatalogDocumentRevision | None = None
            if exact_revision_id is not None:
                if (
                    not isinstance(exact_revision_id, str)
                    or not 1 <= len(exact_revision_id.strip()) <= 256
                ):
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_REVISION_INVALID,
                        "exact recipe revision is invalid",
                    )
                exact = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.id == exact_revision_id.strip(),
                    )
                )
                if exact is None or exact.document_id != seed.document_id:
                    raise ModelCacheStorageUnknown(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
                        "selected recipe revision was not found",
                    )
            elif re.fullmatch(_DIGEST_PATTERN, identity) or (
                re.fullmatch(UUID_PATTERN, identity) and seed.id == identity
            ):
                exact = seed

            revisions = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.document_id == seed.document_id,
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.revision_number.desc(),
                        CatalogDocumentRevision.id.desc(),
                    )
                )
            )
            if not revisions and exact is None:
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.RECIPE_REVISION_MISSING,
                    "recipe has no active revision",
                )
            if exact is not None:
                selection_pool = [exact]
            else:
                selection_pool = revisions

            unreadable_candidates = False

            def compatible_model(
                revision: CatalogDocumentRevision,
            ) -> tuple[str, str | None]:
                nonlocal unreadable_candidates
                readable = cache._readable_content_revision(session, revision)
                if readable is None:
                    unreadable_candidates = True
                    return "", None
                try:
                    recipe = read_catalog_document(readable)
                except CatalogRevisionContractError:
                    unreadable_candidates = True
                    return "", None
                if not isinstance(recipe, RecipeDefinition):
                    raise ModelCacheStorageUnknown(
                        ModelCacheCode.RECIPE_INVALID,
                        "recipe revision is not canonical",
                    )
                digests = _recipe_model_content_digests(recipe)
                if requested_model is not None and requested_model not in digests:
                    return "", None
                for digest in digests if requested_model is None else [requested_model]:
                    model_revision = session.scalar(
                        select(CatalogDocumentRevision)
                        .where(
                            CatalogDocumentRevision.kind == "model",
                            CatalogDocumentRevision.content_digest == digest,
                        )
                        .order_by(
                            (CatalogDocumentRevision.state == "active").desc(),
                            CatalogDocumentRevision.revision_number.desc(),
                            CatalogDocumentRevision.id.desc(),
                        )
                    )
                    if model_revision is None:
                        unreadable_candidates = True
                        continue
                    readable_model = cache._readable_content_revision(
                        session, model_revision
                    )
                    if readable_model is None:
                        unreadable_candidates = True
                        continue
                    try:
                        model = read_catalog_document(readable_model)
                    except CatalogRevisionContractError:
                        unreadable_candidates = True
                        continue
                    if not isinstance(model, ModelDefinition):
                        continue
                    variant = model.identity.variant
                    if requested_variant is None or variant == requested_variant:
                        return digest, variant
                return "", None

            def verified_image(revision_id: str) -> RevisionImage | None:
                # SQL names the images the recipe's builds produced; managed
                # storage owns whether an archive is present. The build row
                # carries the archive identity, so no receipt row joins them.
                if cache._runtime_archive_available is None:
                    return None
                images = revision_images(session, [revision_id], same_source=True)
                for image in images.get(revision_id, ()):
                    try:
                        present = cache._runtime_archive_available(
                            image.archive_sha256, image.image_bytes
                        )
                    except OSError:
                        present = False
                    if present:
                        return image
                return None

            selected: (
                tuple[
                    CatalogDocumentRevision,
                    str,
                    str | None,
                    RevisionImage | None,
                ]
                | None
            ) = None
            newest_compatible: CatalogDocumentRevision | None = None
            for revision in selection_pool:
                digest, variant = compatible_model(revision)
                if not digest:
                    continue
                newest_compatible = newest_compatible or revision
                receipt = verified_image(revision.id)
                if receipt is not None:
                    selected = (revision, digest, variant, receipt)
                    break
            if selected is None:
                if newest_compatible is None:
                    if unreadable_candidates:
                        raise ModelCacheStorageUnknown(
                            ModelCacheCode.RECIPE_REVISION_MISSING,
                            "exact candidate content is not observable",
                        )
                    raise ModelCacheStorageUnknown(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
                        "requested model variant content is not observable",
                    )
                revision = newest_compatible
                digest, variant = compatible_model(revision)
                receipt = None
            else:
                revision, digest, variant, receipt = selected

            model_rows = list(
                session.scalars(
                    select(ModelCacheSet).where(
                        ModelCacheSet.model_content_sha256 == digest,
                        ModelCacheSet.state == "cached",
                    )
                )
            )
            # A set is keyed by its bytes and keeps the provenance of the
            # revision that cached it first: a later model revision with the
            # same files is cached under that row, as compilation sees it.
            try:
                shared = session.get(
                    ModelCacheSet,
                    cache.resolve_artifact_set(recipe_revision_id=revision.id).digest,
                )
            except ModelCacheError:
                shared = None
            if (
                shared is not None
                and shared.state == "cached"
                and shared not in model_rows
            ):
                model_rows.append(shared)

            def model_set_available(row: ModelCacheSet) -> bool:
                manifest = cache._stored_manifest(row)
                if manifest is None:
                    return False  # unknown: this set is skipped, not a blocker
                if manifest.digest != row.artifact_set_sha256:
                    return False  # damaged derived metadata is a cache miss
                required = set(_unique_artifacts(manifest.artifacts))
                return required <= cache._managed_cached_objects(manifest)

            model_set = max(
                (row for row in model_rows if model_set_available(row)),
                key=lambda item: (item.updated_at, item.artifact_set_sha256),
                default=None,
            )
            model_cached = model_set is not None
            model_expected = model_set.expected_bytes if model_set is not None else None
            model_verified = model_set.verified_bytes if model_set is not None else None

            recipe_cached = receipt is not None
            image_bytes = receipt.image_bytes if receipt is not None else None
            image_digest = receipt.image_digest if receipt is not None else None
            latest = next(
                (
                    item
                    for item in revisions
                    if item.revision_number > revision.revision_number
                ),
                None,
            )
            additional = (
                model_expected + image_bytes
                if isinstance(model_expected, int) and isinstance(image_bytes, int)
                else None
            )
            blockers = []
            if not recipe_cached:
                blockers.append(ModelCacheBlockerCode.RECIPE_NOT_CACHED)
            if not model_cached:
                blockers.append(ModelCacheBlockerCode.MODEL_NOT_CACHED)
            return CacheResolution(
                recipe=CachedRecipeResolution(
                    recipe_revision_id=revision.id,
                    document_id=revision.document_id,
                    publisher=revision.publisher,
                    slug=revision.slug,
                    revision_number=revision.revision_number,
                    content_sha256=revision.content_digest,
                    cached=recipe_cached,
                    cache_state="cached" if recipe_cached else "missing",
                    artifact_set_sha256=receipt.archive_sha256 if receipt else None,
                    expected_bytes=image_bytes,
                    verified_bytes=image_bytes,
                    image_digest=image_digest,
                    update_available=latest is not None,
                ),
                model=CachedModelResolution(
                    content_sha256=digest,
                    cached=model_cached,
                    cache_state="cached" if model_cached else "missing",
                    artifact_set_sha256=(
                        model_set.artifact_set_sha256 if model_set else None
                    ),
                    expected_bytes=model_expected,
                    verified_bytes=model_verified,
                    variant=variant,
                ),
                resources=CachedResourceEstimate(
                    additional_disk_bytes=additional,
                    model_bytes=model_expected,
                    image_bytes=image_bytes,
                ),
                blockers=blockers,
            )
