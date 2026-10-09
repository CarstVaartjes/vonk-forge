"""Inventory."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    CacheReferenceReason,
    ModelCacheCode,
    ModelFileState,
    RunState,
)

from ..bounded_retry import bounded_attempts
from ..catalog_queries import active_head_revision
from ..categorized_errors import InvalidValue
from ..fleet_profile_contract import FleetProfileAssignmentInput
from ..lifecycle.evidence import Residue
from ..machine_states import INSTALLATION_ACTIVE
from ..model_cache_contract import CacheManifest
from ..models import (
    CatalogDocumentRevision,
    FleetProfile,
    ModelCacheSet,
    RecipeInstallation,
    RecipeRun,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .artifacts import ArtifactSetManifest, ArtifactSpec, _optional_digest
from .catalog_helpers import _datetime, _iso, _parse_iso, _recipe_model_content_digests
from .constants import SCHEMA_VERSION, SOURCE_POLICY
from .errors import (
    ModelCacheError,
    ModelCacheStorageUnknown,
)
from .source_helpers import _contains_digest

if TYPE_CHECKING:
    from .service import ModelCacheService


class InventoryMixin:
    """Inventory behavior of the cache service."""

    def get_entry(self, artifact_set_sha256: str) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise InvalidValue("cache entry digest is required")
        cache.reconcile_storage()
        for _attempt in bounded_attempts():
            entry = cache._entry(digest)
            if entry is not None:
                return entry
        raise ModelCacheStorageUnknown(
            ModelCacheCode.ENTRY_MISSING, "exact cache entry is not observable"
        )

    def _entry(self, digest: str) -> dict[str, object] | None:
        """One entry's projection; ``None`` when the set is not (or no longer) held."""
        cache = cast("ModelCacheService", self)

        with cache._session(write=True) as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                return None
            cache._refresh_protection(session, row)
            # The set projection is recomputed when an entry is read rather
            # than after every object, so a partially completed operation
            # still reports exact stored bytes without making publication
            # quadratic in the set's own membership.
            row.verified_bytes = cache._verified_bytes(session, row.artifact_set_sha256)
            # A manifest that does not read (and cannot be re-derived) lists no
            # artifacts: the entry still reports, and a repair or a new request
            # for the set restores its description.
            manifest = cache._stored_manifest(row)
            artifacts = []
            unique_bytes = 0
            seen: set[str] = set()
            for spec in manifest.artifacts if manifest is not None else ():
                # One owner per fact: managed storage decides availability, and
                # the same descriptor check reports the stored length, so a
                # receipt alone cannot invent bytes.
                stored = cache._stored_object(spec.sha256, spec.expected_bytes)
                actual = stored or 0
                state = (
                    ModelFileState.VERIFIED
                    if stored is not None
                    else ModelFileState.MISSING
                )
                if stored is not None and spec.sha256 not in seen:
                    unique_bytes += spec.expected_bytes
                    seen.add(spec.sha256)
                artifacts.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "key": spec.key,
                        "id": spec.artifact_id,
                        "path": spec.path,
                        "sha256": spec.sha256,
                        "expected_bytes": spec.expected_bytes,
                        "actual_bytes": actual,
                        "roles": list(spec.roles),
                        "state": state,
                        "source": spec.source,
                    }
                )
            update = (
                cache._update_flags(session, row, manifest)
                if manifest is not None
                else (False, False)
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "artifact_set_sha256": digest,
                "model_content_sha256": row.model_content_sha256,
                "recipe_revision_sha256": row.recipe_revision_sha256,
                "state": row.state,
                "coverage": "complete" if row.state == "cached" else "incomplete",
                "expected_bytes": row.expected_bytes,
                "verified_bytes": row.verified_bytes,
                "unique_bytes": unique_bytes,
                "artifacts": artifacts,
                "protected": bool(row.protected),
                "protected_reasons": list(row.protected_reasons or ()),
                "update_available": update[0],
                "recipe_update_available": update[1],
                "created_at": _iso(row.created_at) or "",
                "updated_at": _iso(row.updated_at) or "",
                "verified_at": _iso(row.verified_at),
                "last_error": row.last_error,
            }

    def inventory(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 100:
            raise InvalidValue("cache entry limit is invalid")
        cache.reconcile_storage()
        with cache._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheSet).order_by(
                        ModelCacheSet.updated_at.desc(),
                        ModelCacheSet.artifact_set_sha256.desc(),
                    )
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if (
                    _datetime(row.updated_at) == boundary_time
                    and row.artifact_set_sha256 == boundary[1]
                ):
                    start = index + 1
                    break
            else:
                # A cursor remains a seek position after its row changes or
                # disappears. Reading the next page needs no retained owner.
                start = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if (_datetime(row.updated_at), row.artifact_set_sha256)
                        < (boundary_time, boundary[1])
                    ),
                    total,
                )
        page = rows[start : start + limit]
        entries = [
            entry
            for entry in (cache._entry(row.artifact_set_sha256) for row in page)
            if entry is not None  # removed since the page was read
        ]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "entries": entries,
            "storage": cache.storage_summary().document(),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def reconcile_storage(self) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        with cache._lock:
            # Object availability lives in managed storage. Reconciliation
            # rewrites a lost receipt for a same-size object, and removes the receipt of
            # an object whose bytes are gone so admission cannot admit it.
            with cache._session() as session:
                sets = list(session.scalars(select(ModelCacheSet)))
                expected: dict[str, ArtifactSpec] = {}
                for row in sets:
                    stored = cache._stored_manifest(row)
                    if stored is None:
                        continue  # unknown set: skipped, the rest reconcile
                    for spec in stored.artifacts:
                        expected[spec.sha256] = spec
            for sha256, spec in expected.items():
                path = cache._object_path(sha256)
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    cache._receipt_path(sha256).unlink(missing_ok=True)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    cache._receipt_path(sha256).unlink(missing_ok=True)
                    continue
                available = cache._object_is_available(sha256, spec.expected_bytes)
                if not available:
                    cache._receipt_path(sha256).unlink(missing_ok=True)
                if not available and metadata.st_size == spec.expected_bytes:
                    cache._write_object_receipt(spec, cache._clock())
            with cache._session(write=True) as session:
                sets = list(session.scalars(select(ModelCacheSet)))
                for row in sets:
                    previous = (
                        row.state,
                        row.verified_bytes,
                        row.protected,
                        row.protected_reasons,
                    )
                    manifest = cache._stored_manifest(row)
                    if manifest is None:
                        continue  # unknown set: skipped, the rest reconcile
                    verified = cache._verified_bytes(session, row.artifact_set_sha256)
                    row.verified_bytes = verified
                    if row.state not in {"downloading", "verifying"}:
                        row.state = (
                            "cached"
                            if cache._manifest_coverage_complete(manifest)
                            else "needs-repair"
                        )
                    cache._refresh_protection(session, row)
                    if previous != (
                        row.state,
                        row.verified_bytes,
                        row.protected,
                        row.protected_reasons,
                    ):
                        row.updated_at = cache._clock()
            return cache.storage_summary().document()

    def _refresh_protection(self, session: Session, row: ModelCacheSet) -> None:
        # Protection is a projection of durable references. Recompute it from
        # those references so removal of the last reference makes a set
        # evictable without retaining an old flag.
        cache = cast("ModelCacheService", self)
        reasons: set[str] = set()
        if row.model_content_sha256 is not None:
            cache_model_digests = cache._cache_model_content_digests(row)
            installations = session.scalars(
                select(RecipeInstallation).where(
                    RecipeInstallation.state.in_(INSTALLATION_ACTIVE),
                )
            )
            if any(
                cache._recipe_references_cache(
                    session, installation.recipe_revision_id, cache_model_digests
                )
                for installation in installations
            ):
                reasons.add(CacheReferenceReason.RECIPE_INSTALLATION)
            running_revision_ids = session.scalars(
                select(RecipeInstallation.recipe_revision_id)
                .join(RecipeRun, RecipeRun.installation_id == RecipeInstallation.id)
                .where(
                    RecipeRun.state.in_(
                        [RunState.PLANNED, RunState.STARTING, RunState.RUNNING]
                    )
                )
            )
            if any(
                cache._recipe_references_cache(
                    session, revision_id, cache_model_digests
                )
                for revision_id in running_revision_ids
            ):
                reasons.add(CacheReferenceReason.RUNNING_MODEL)
        for profile in session.scalars(select(FleetProfile)):
            assignments = read_row_column(profile, "assignments")
            if (
                isinstance(assignments, Residue)
                or _contains_digest(
                    serialize_json_value(assignments),
                    row.model_content_sha256,
                    row.recipe_revision_sha256,
                )
                or cache._profile_references_cache(session, profile, row)
            ):
                reasons.add(CacheReferenceReason.SAVED_PROFILE)
        row.protected = bool(reasons)
        row.protected_reasons = sorted(reasons)

    @staticmethod
    def _cache_model_content_digests(row: ModelCacheSet) -> set[str]:
        try:
            stored = read_row_column(row, "manifest")
            if not isinstance(stored, CacheManifest):
                return {row.model_content_sha256} if row.model_content_sha256 else set()
            manifest = ArtifactSetManifest.from_contract(stored)
        except ModelCacheError:
            return (
                {row.model_content_sha256}
                if row.model_content_sha256 is not None
                else set()
            )
        return set(manifest.model_content_digests) or (
            {row.model_content_sha256}
            if row.model_content_sha256 is not None
            else set()
        )

    def _recipe_references_cache(
        self,
        session: Session,
        revision_id: str,
        cache_model_digests: set[str],
    ) -> bool:
        cache = cast("ModelCacheService", self)
        try:
            # Protection follows exact content, including identical content
            # recovered from another provenance record.
            recipe, _, _ = cache._recipe_document(session, None, revision_id)
            direct_model_digests = set(_recipe_model_content_digests(recipe))
            if cache_model_digests.intersection(direct_model_digests):
                return True
            model_rows: dict[str, CatalogDocumentRevision] = {}
            for digest in direct_model_digests:
                cache._collect_model_definitions(session, digest, model_rows)
        except ModelCacheError:
            # An unreadable live reference cannot prove these bytes unused.
            # Retain them until exact content is observable; reads and new
            # preparation continue without adopting a different catalog head.
            return True
        return bool(cache_model_digests.intersection(model_rows))

    def _profile_references_cache(
        self,
        session: Session,
        profile: FleetProfile,
        row: ModelCacheSet,
    ) -> bool:
        """Resolve profile recipe IDs before deciding a cache set is evictable."""
        cache = cast("ModelCacheService", self)
        assignments = read_row_column(profile, "assignments")
        if not isinstance(assignments, list):
            return isinstance(
                assignments, Residue
            )  # destructive cleanup retains unknown references
        for assignment in assignments:
            if not isinstance(assignment, FleetProfileAssignmentInput):
                continue
            publisher, separator, slug = assignment.recipe_selector.partition("/")
            if not separator:
                publisher, slug = "vonk-forge", publisher
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.publisher == publisher,
                    CatalogDocumentRevision.slug == slug,
                    CatalogDocumentRevision.state == "active",
                    active_head_revision(),
                )
            )
            if (
                revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
            ):
                continue
            if (
                row.recipe_revision_sha256 is not None
                and revision.content_digest == row.recipe_revision_sha256
            ):
                return True
            if row.model_content_sha256 is None:
                continue
            if cache._recipe_references_cache(
                session, revision.id, cache._cache_model_content_digests(row)
            ):
                return True
        return False

    def _update_flags(
        self, session: Session, row: ModelCacheSet, manifest: ArtifactSetManifest
    ) -> tuple[bool, bool]:
        cache = cast("ModelCacheService", self)
        model_update = cache._model_update_candidate(session, manifest) is not None
        recipe_update = False
        if row.recipe_revision_sha256 is not None:
            latest_recipe = cache._latest_recipe_digest(
                session, row.recipe_revision_sha256
            )
            recipe_update = (
                latest_recipe is not None
                and latest_recipe != row.recipe_revision_sha256
            )
        return model_update, recipe_update
