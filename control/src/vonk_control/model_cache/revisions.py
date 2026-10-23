"""Release superseded model sets through the existing fenced collector."""

from __future__ import annotations

import stat
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName

from ..admission_locking import AdmissionLockBusy, AdmissionRowLock, lock_admission_rows
from ..artifact_lifecycle import ArtifactReferenceUnsettled, ArtifactReferenceUnverified
from ..artifact_reference_scan import model_set_reference_reasons
from ..catalog_queries import active_head_revision
from ..logging import log_event
from ..model_cache_contract import (
    CacheOperationKind,
    ModelCacheSetState,
    ModelCacheUsage,
    RetainedModelRevision,
)
from ..models import CatalogDocumentRevision, ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSetManifest
from .catalog_helpers import _revision_identity
from .constants import _LOGGER
from .errors import ModelCacheConflictUnknown, ModelCacheError
from .persistence import _manifest_of

if TYPE_CHECKING:
    from .service import ModelCacheService

_REVISION_SCAN_INTERVAL = timedelta(seconds=30)


class _RevisionChanged(Exception):
    """Re-plan on the next worker pass; no removal was accepted."""


class RevisionsMixin:
    def _model_revision_sets(
        self,
        session: Session,
    ) -> tuple[
        list[tuple[CatalogDocumentRevision, ModelCacheSet, ArtifactSetManifest]],
        dict[str, set[str]],
    ]:
        cache = cast("ModelCacheService", self)
        revisions = list(
            session.scalars(
                select(CatalogDocumentRevision)
                .options(
                    defer(CatalogDocumentRevision.document),
                    defer(CatalogDocumentRevision.projected),
                )
                .where(
                    CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                )
                .order_by(
                    CatalogDocumentRevision.revision_number.desc(),
                    CatalogDocumentRevision.created_at.desc(),
                    CatalogDocumentRevision.id.desc(),
                )
            )
        )
        by_content = {}
        for revision in revisions:
            by_content.setdefault(revision.content_digest, revision)
        rows = list(session.scalars(select(ModelCacheSet)))
        bindings: dict[str, set[str]] = defaultdict(set)
        result = []
        for row in rows:
            manifest = cache._stored_manifest(row)
            if manifest is None:
                continue
            bindings[row.artifact_set_sha256].update(
                reference.content_sha256 for reference in manifest.verified_models
            )
            if row.model_content_sha256 is not None:
                bindings[row.artifact_set_sha256].add(row.model_content_sha256)
            aliases: dict[tuple[str, str], list[CatalogDocumentRevision]] = defaultdict(
                list
            )
            for content in bindings[row.artifact_set_sha256]:
                candidate = by_content.get(content)
                if candidate is not None:
                    aliases[(candidate.publisher, candidate.slug)].append(candidate)
            for candidates in aliases.values():
                revision = max(
                    candidates,
                    key=lambda candidate: (
                        candidate.revision_number,
                        candidate.created_at,
                        candidate.id,
                    ),
                )
                result.append((revision, row, manifest))
        return sorted(
            result,
            key=lambda item: (item[0].revision_number, item[0].created_at, item[0].id),
            reverse=True,
        ), bindings

    def _remember_verified_models(
        self, session: Session, row: ModelCacheSet, incoming: ArtifactSetManifest
    ) -> None:
        """Publish verified bindings with the artifact, independent of job-history retention.

        Keep the newest binding per publisher/slug, preserving the first
        provenance fields and exact physical identity of the reusable set.
        """
        cache = cast("ModelCacheService", self)
        try:
            with session.no_autoflush:
                locked = lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "binding",
                            ModelCacheSet,
                            select(ModelCacheSet).where(
                                ModelCacheSet.artifact_set_sha256
                                == row.artifact_set_sha256,
                            ),
                        ),
                    ),
                )["binding"]
        except AdmissionLockBusy as error:
            raise ModelCacheConflictUnknown(error.code, str(error)) from error
        if not locked:
            return
        row = cast(ModelCacheSet, locked[0])
        stored = cache._stored_manifest(row)
        if stored is None or incoming.digest != row.artifact_set_sha256:
            return
        contents = {reference.content_sha256 for reference in stored.verified_models}
        if incoming.model_content_sha256 is not None:
            contents.add(incoming.model_content_sha256)
        elected: dict[tuple[str, str], CatalogDocumentRevision] = {}
        for revision in session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                CatalogDocumentRevision.content_digest.in_(contents),
            )
            .order_by(
                CatalogDocumentRevision.revision_number.desc(),
                CatalogDocumentRevision.created_at.desc(),
                CatalogDocumentRevision.id.desc(),
            )
        ):
            elected.setdefault((revision.publisher, revision.slug), revision)
        references = tuple(
            reference
            for key in sorted(elected)
            if (reference := _revision_identity(elected[key])) is not None
        )
        if references != stored.verified_models:
            updated = replace(stored, verified_models=references)
            row.manifest = ArtifactSetManifest.from_contract(
                updated.contract()
            ).document()

    def _reconcile_verified_models(self) -> None:
        """Replay completed current downloads into the same manifest publisher.

        Readers only use the manifest. This repairs a missing publication;
        historical operations may subsequently disappear without losing it.
        """
        cache = cast("ModelCacheService", self)
        pending: list[ArtifactSetManifest] = []
        with cache._session() as session:
            rows = {
                row.artifact_set_sha256: row
                for row in session.scalars(select(ModelCacheSet))
            }
            content = ModelCacheOperation.payload["manifest"][
                "model_content_sha256"
            ].as_string()
            # Any one successful request proves an identical set/content pair;
            # the representative UUID is not used to elect the newest revision.
            proofs = session.execute(
                select(
                    func.min(ModelCacheOperation.id),
                    ModelCacheOperation.artifact_set_sha256,
                    content,
                )
                .where(
                    ModelCacheOperation.artifact_set_sha256.in_(rows),
                    ModelCacheOperation.kind.in_(
                        (CacheOperationKind.DOWNLOAD, CacheOperationKind.REPAIR)
                    ),
                    ModelCacheOperation.state == LifecycleState.SUCCEEDED,
                )
                .group_by(ModelCacheOperation.artifact_set_sha256, content)
            ).all()
            for operation_id, set_digest, digest in proofs:
                if set_digest is None or operation_id is None or digest is None:
                    continue
                row = rows.get(set_digest)
                if row is None or row.state != ModelCacheSetState.CACHED:
                    continue
                stored = cache._stored_manifest(row)
                if stored is None or digest in {
                    reference.content_sha256 for reference in stored.verified_models
                }:
                    continue
                operation = session.get(ModelCacheOperation, operation_id)
                payload = (
                    cache._transfer_or_none(operation)
                    if operation is not None
                    else None
                )
                if payload is not None:
                    manifest = _manifest_of(payload)
                    if manifest.digest == set_digest:
                        pending.append(manifest)
        for manifest in pending:
            if not cache._manifest_coverage_complete(manifest):
                continue
            try:
                with cache._session(write=True) as session:
                    row = session.get(ModelCacheSet, manifest.digest)
                    if row is not None and row.state == ModelCacheSetState.CACHED:
                        cache._remember_verified_models(session, row, manifest)
            except ModelCacheConflictUnknown as error:
                log_event(
                    _LOGGER,
                    "model_cache.binding_recheck",
                    service="control-worker",
                    set_digest=manifest.digest,
                    code=error.code,
                    detail=error.detail,
                    next_attempt_at=cache._revision_scan_at.isoformat()
                    if cache._revision_scan_at is not None
                    else None,
                )

    def _adopt_current_model_bindings(self) -> None:
        """Recover editorial bindings by exact canonical identity, not history.

        This only updates an existing verified set; it cannot resurrect a
        removed cache entry, transfer bytes or authorize a workload.
        """
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            held = set(
                session.scalars(
                    select(ModelCacheSet.artifact_set_sha256).where(
                        ModelCacheSet.state == ModelCacheSetState.CACHED
                    )
                )
            )
            if not held:
                return
            current = list(
                session.execute(
                    select(CatalogDocumentRevision.content_digest).where(
                        CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                        active_head_revision(),
                    )
                ).scalars()
            )
            bound = {
                reference.content_sha256
                for row in session.scalars(select(ModelCacheSet))
                if (manifest := cache._stored_manifest(row)) is not None
                for reference in manifest.verified_models
            }
        for digest in set(current) - bound:
            try:
                manifest = cache.resolve_artifact_set(model_content_sha256=digest)
                if manifest.digest in held and cache._manifest_coverage_complete(
                    manifest
                ):
                    cache.adopt_verified_set(manifest)
            except ModelCacheError as error:
                log_event(
                    _LOGGER,
                    "model_cache.binding_recheck",
                    service="control-worker",
                    model_content_sha256=digest,
                    code=error.code,
                    detail=error.detail,
                    next_attempt_at=cache._revision_scan_at.isoformat()
                    if cache._revision_scan_at is not None
                    else None,
                )

    def reconcile_model_revisions(self) -> None:
        """Reconsider retained owners every 30 seconds, including after restart.

        No in-use set gets fenced. Acceptance rechecks the exact successor and
        owners under the normal set/object gates; conflicts get a fresh plan.
        The existing removal worker owns bounded, crash-safe byte collection.
        """
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        if cache._revision_scan_at is not None and now < cache._revision_scan_at:
            return
        cache._revision_scan_at = now + _REVISION_SCAN_INTERVAL
        cache._reconcile_verified_models()
        cache._adopt_current_model_bindings()
        with cache._session() as session:
            sets, _bindings = cache._model_revision_sets(session)
        newest: dict[
            tuple[str, str], tuple[CatalogDocumentRevision, ModelCacheSet]
        ] = {}
        by_set: dict[str, list[CatalogDocumentRevision]] = defaultdict(list)
        ready_sets: dict[str, bool] = {}
        for revision, row, manifest in sets:
            by_set[row.artifact_set_sha256].append(revision)
            lineage = (revision.publisher, revision.slug)
            if lineage not in newest and row.state == ModelCacheSetState.CACHED:
                ready = ready_sets.get(row.artifact_set_sha256)
                if ready is None:
                    ready = cache._manifest_coverage_complete(manifest)
                    ready_sets[row.artifact_set_sha256] = ready
                if ready:
                    newest[lineage] = (revision, row)
        candidates: dict[str, set[str]] = {}
        for old_set, revisions in by_set.items():
            replacements: set[str] = set()
            for revision in revisions:
                successor = newest.get((revision.publisher, revision.slug))
                if (
                    successor is None
                    or successor[0].content_digest == revision.content_digest
                    or successor[0].revision_number <= revision.revision_number
                ):
                    break  # This physical set still owns a current model revision.
                replacements.add(successor[1].artifact_set_sha256)
            else:
                candidates[old_set] = replacements
        if not candidates:
            return
        try:
            with cache._session() as session:
                owners = model_set_reference_reasons(session, tuple(candidates))
                candidates = {
                    digest: replacements
                    for digest, replacements in candidates.items()
                    if not owners[digest]
                }
                scope = cache._model_removal_scope_for_sets(session, tuple(candidates))
        except (
            ArtifactReferenceUnsettled,
            ArtifactReferenceUnverified,
            ModelCacheConflictUnknown,
        ):
            return  # Snapshot changed: the next pass derives a fresh exact scope.
        # Connect only sets sharing objects that will have no remaining owner.
        # Shared objects still held by a current/in-use set do not join unrelated
        # cleanup work. One accepted scope then owns every obsolete shared byte.
        components = {digest: {digest} for digest in candidates}
        reclaimable = set(scope.delete_objects)
        object_owners: dict[str, set[str]] = defaultdict(set)
        for digest, objects in scope.memberships:
            for object_digest in objects:
                if object_digest in reclaimable:
                    object_owners[object_digest].add(digest)
        for sharing in object_owners.values():
            joined = set().union(*(components[digest] for digest in sharing))
            for digest in joined:
                components[digest] = joined
        for selected in {frozenset(group) for group in components.values()}:
            replacements = set().union(*(candidates[digest] for digest in selected))
            cache._release_model_revision(selected, replacements)

    def _release_model_revision(
        self, selected_sets: frozenset[str], replacements: set[str]
    ) -> None:
        cache = cast("ModelCacheService", self)

        def verify(session: Session, selected: tuple[str, ...]) -> None:
            for new_set in replacements:
                latest = session.get(ModelCacheSet, new_set)
                if latest is None or latest.state != ModelCacheSetState.CACHED:
                    raise _RevisionChanged
            if any(model_set_reference_reasons(session, selected).values()):
                raise _RevisionChanged

        try:
            with cache._lock, cache._session(write=True) as session:
                cache._accept_model_removal(
                    session,
                    actor="model-revision-retention",
                    request_key=str(uuid.uuid4()),
                    selector=min(selected_sets),
                    model_content_sha256=None,
                    selected_sets=tuple(sorted(selected_sets)),
                    verify=verify,
                )
        except _RevisionChanged:
            return  # retained owners are explained by model_storage
        except (
            ModelCacheConflictUnknown,
            ArtifactReferenceUnsettled,
            ArtifactReferenceUnverified,
        ) as error:
            # Another owner or removal won. Release all gates and re-plan
            # from current sets on the next bounded scan, never spin.
            log_event(
                _LOGGER,
                "model_cache.revision_recheck",
                service="control-worker",
                set_digests=sorted(selected_sets),
                code=error.code,
                detail=error.detail,
                next_attempt_at=cache._revision_scan_at.isoformat()
                if cache._revision_scan_at is not None
                else None,
            )
            return

    def model_storage(self) -> dict[str, ModelCacheUsage]:
        """Observe storage outside request handling for the disposable Library index."""
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            sets, _ = cache._model_revision_sets(session)
            catalog_lineages = list(
                session.execute(
                    select(
                        CatalogDocumentRevision.content_digest,
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                    ).where(
                        CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL
                    )
                )
            )
            reasons = model_set_reference_reasons(
                session, (row.artifact_set_sha256 for _, row, _ in sets)
            )
        lineages: dict[str, tuple[str, str]] = {}
        objects: dict[str, int] = {}
        holders: dict[str, set[tuple[str, str]]] = defaultdict(set)
        content_sets: dict[str, set[str]] = defaultdict(set)
        object_sets: dict[str, set[str]] = defaultdict(set)
        set_lineages: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for revision, row, _ in sets:
            set_lineages[row.artifact_set_sha256].add(
                (revision.publisher, revision.slug)
            )
        for revision, row, manifest in sets:
            lineage = (revision.publisher, revision.slug)
            lineages[revision.content_digest] = lineage
            content_sets[revision.content_digest].add(row.artifact_set_sha256)
            for spec in manifest.artifacts:
                if spec.sha256 not in objects:
                    try:
                        metadata = cache._object_path(spec.sha256).lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        objects[spec.sha256] = metadata.st_size
                if spec.sha256 in objects:
                    holders[spec.sha256].add(lineage)
                    object_sets[spec.sha256].add(row.artifact_set_sha256)
        usage = {
            lineage: ModelCacheUsage(disk_bytes=0, unique_bytes=0, shared_bytes=0)
            for lineage in lineages.values()
        }
        for digest, owners in holders.items():
            size = objects[digest]
            usage[min(owners)].disk_bytes += size
            for owner in owners:
                if len(owners) == 1:
                    usage[owner].unique_bytes += size
                else:
                    usage[owner].shared_bytes += size
        seen: dict[tuple[str, str], str] = {}
        retained: set[str] = set()
        for revision, _, _ in sets:
            lineage = (revision.publisher, revision.slug)
            latest_content = seen.setdefault(lineage, revision.content_digest)
            if (
                revision.content_digest != latest_content
                and revision.content_digest not in retained
            ):
                owners = sorted(
                    {
                        reason
                        for digest in content_sets[revision.content_digest]
                        for reason in reasons[digest]
                    }
                    | {
                        f"cached model {publisher}/{slug}"
                        for digest in content_sets[revision.content_digest]
                        for publisher, slug in set_lineages[digest] - {lineage}
                    }
                )
                if owners:
                    usage[lineage].retained_revisions.append(
                        RetainedModelRevision(
                            content_sha256=revision.content_digest,
                            unique_bytes=sum(
                                size
                                for digest, size in objects.items()
                                if object_sets[digest]
                                <= content_sets[revision.content_digest]
                                and holders[digest] == {lineage}
                            ),
                            owners=owners,
                        )
                    )
                retained.add(revision.content_digest)
        result: dict[str, ModelCacheUsage] = {}
        for content, publisher, slug in catalog_lineages:
            held = usage.get((publisher, slug))
            if held is not None:
                result[content] = held.model_copy()
        return result
