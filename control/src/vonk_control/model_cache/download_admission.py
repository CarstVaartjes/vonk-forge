"""Download admission."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    ModelCacheBlockerCode,
    ModelCacheCode,
)

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactReferenceUnsettled,
    has_pending_removal,
    lock_reference_gates,
)
from ..bounded_json import require_sequence
from ..lifecycle.model_cache import ModelCacheAdapter
from ..model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheRetry,
    ModelCacheTransfer,
)
from ..model_cache_progress import progress_document
from ..models import ModelCacheOperation
from ..settings import DATABASE_WAIT_BUDGETS
from ..strict_json import serialize_json_value
from .artifacts import (
    ArtifactSetManifest,
    _optional_digest,
    _sha256_json,
    _split_transient_bytes,
    _unique_artifacts,
)
from .constants import SCHEMA_VERSION, SOURCE_POLICY
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictRefused,
    ModelCacheConflictUnknown,
)
from .persistence import _cache_failure, _write_operation_payload
from .source_helpers import _model_selector, _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class DownloadAdmissionMixin:
    """Download admission behavior of the cache service."""

    def start_download(
        self,
        *,
        actor: str,
        request_key: str,
        plan_digest: str,
        artifact_set_sha256: str | None = None,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        selector: str | None = None,
        artifacts: Sequence[object] | None = None,
        force: bool = False,
        interrupt_after_bytes: int | None = None,
    ) -> CacheOperationView:
        cache = cast("ModelCacheService", self)
        request_key = _request_key(request_key)
        if selector is not None:
            selector = _model_selector(selector)
            with cache._session() as session:
                replay = cache._download_replay(
                    session, request_key, actor=actor, selector=selector, force=force
                )
                if replay is not None:
                    return replay
        requested_plan = _optional_digest(plan_digest)
        if requested_plan is None:
            raise ModelCacheConflictRefused(
                ModelCacheCode.PLAN_INVALID, "download plan digest is invalid"
            )
        manifest = cache._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        set_digest = manifest.digest
        # A retry of the same idempotency key must return the original
        # operation even when its partial checkpoint has changed the current
        # preview's remaining-byte estimate.
        with cache._session() as session:
            replay = cache._download_replay(
                session,
                request_key,
                actor=actor,
                selector=selector,
                force=force,
                artifact_set_sha256=set_digest,
                plan_digest=requested_plan,
            )
            if replay is not None:
                return replay
        preview = cache._download_preview_for_manifest(manifest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.STALE_PLAN, "download preview is stale"
            )
        waiting_for = "; ".join(
            str(item)
            for item in require_sequence(preview["blockers"], "download blockers")
        )
        planned = None if force else preview.get("_transfer")
        transfer = (
            planned
            if isinstance(planned, ModelCacheTransfer)
            else cache._transfer_state_for_manifest(manifest, force=force)
        )
        capacity = cache._capacity_wait(waiting_for)
        wait_until = None if capacity is None else capacity[1]
        payload = _write_operation_payload(
            "download",
            ModelCacheDownloadPayload(
                schema_version=SCHEMA_VERSION,
                source_policy=SOURCE_POLICY,
                artifact_set_sha256=set_digest,
                manifest=manifest.contract(),
                plan_digest=requested_plan,
                transfer=transfer,
                retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                force_refresh=force,
                selector=selector,
                operator_action="download-model" if selector is not None else None,
                failure=None if capacity is None else capacity[0],
            ),
        )
        deadline = (
            time.monotonic()
            + DATABASE_WAIT_BUDGETS.patient_admission_lock_timeout_ms / 1000
        )
        delay = 0.01
        while True:
            try:
                with cache._lock, cache._session(write=True) as session:
                    replay = cache._download_replay(
                        session,
                        request_key,
                        actor=actor,
                        selector=selector,
                        force=force,
                        artifact_set_sha256=set_digest,
                        plan_digest=requested_plan,
                    )
                    if replay is not None:
                        return replay
                    else:
                        now = cache._clock()
                        # Serialize metadata repair under the exact set gate before
                        # reading derived membership. Byte verification stays in storage.
                        lock_reference_gates(
                            session,
                            (ArtifactIdentity("model-set", set_digest),),
                            now=now,
                        )
                        cache._ensure_set(session, manifest)
                        session.flush()
                        cache._require_model_sets_open(
                            session,
                            (set_digest,),
                            now=now,
                            object_digests=tuple(
                                item.sha256 for item in manifest.artifacts
                            ),
                            allow_pending_removal=True,
                        )
                        operation = ModelCacheAdapter.new_operation(
                            request_key=request_key,
                            schema_version=SCHEMA_VERSION,
                            kind="download",
                            next_action_at=wait_until,
                            attempt=1,
                            artifact_set_sha256=set_digest,
                            plan_digest=requested_plan,
                            payload=serialize_json_value(payload),
                            progress=progress_document(
                                cache._progress(
                                    manifest,
                                    phase="queued",
                                    expected_bytes=transfer.total_bytes,
                                )
                            ),
                            actor=actor,
                            created_at=now,
                            updated_at=now,
                        )
                        if has_pending_removal(
                            session,
                            (
                                ArtifactIdentity("model-set", set_digest),
                                *(
                                    ArtifactIdentity("model-object", item.sha256)
                                    for item in manifest.artifacts
                                ),
                            ),
                        ):
                            cache._store_failure(
                                operation,
                                _cache_failure(
                                    ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                                    "Waiting for the prior model removal fence to settle",
                                    retryable=True,
                                    recovery="retry",
                                ),
                            )
                        session.add(operation)
                        session.flush()
                        operation_id = operation.id
            except (ArtifactReferenceUnsettled, ModelCacheConflictUnknown) as error:
                if isinstance(cache._sessions, Session):
                    raise
                # The failed transaction has released every gate. Re-enter normal
                # replay before touching metadata so identical intent is adopted.
                remaining = deadline - time.monotonic()
                if not getattr(error, "retryable", True) or remaining <= 0:
                    with cache._session() as session:
                        replay = cache._download_replay(
                            session,
                            request_key,
                            actor=actor,
                            selector=selector,
                            force=force,
                            artifact_set_sha256=set_digest,
                            plan_digest=requested_plan,
                        )
                        if replay is not None:
                            return replay
                    cache._reference_unknown(error)
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)
                continue
            except IntegrityError:
                # A borrowed transaction belongs to its caller. Only recover here
                # after our own transaction has rolled back and released its locks.
                if isinstance(cache._sessions, Session):
                    raise
                with cache._session() as session:
                    replay = cache._download_replay(
                        session,
                        request_key,
                        actor=actor,
                        selector=selector,
                        force=force,
                        artifact_set_sha256=set_digest,
                        plan_digest=requested_plan,
                    )
                    if replay is None:
                        raise
                    return replay
            break
        if interrupt_after_bytes is not None:
            cache._run_download(
                operation_id,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        return cache.get_operation(operation_id)

    def _download_replay(
        self,
        session: Session,
        request_key: str,
        *,
        actor: str,
        selector: str | None,
        force: bool,
        artifact_set_sha256: str | None = None,
        plan_digest: str | None = None,
    ) -> CacheOperationView | None:
        """Compare original intent, never a refreshed operator preview."""
        cache = cast("ModelCacheService", self)

        existing = session.scalar(
            select(ModelCacheOperation).where(
                ModelCacheOperation.request_key == request_key
            )
        )
        if existing is None:
            return None
        if existing.kind != "download" or existing.actor != actor:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another cache operation",
            )
        payload = cache._payload_or_none(existing)
        if payload is None:
            # The request key names this operation; an unreadable document
            # cannot contradict it, so the replay returns the stored operation.
            return cache._operation_view(existing)
        matches = {
            "selector": payload.selector == selector,
            "refresh": getattr(payload, "force_refresh", False) is force,
        }
        if selector is None:
            matches.update(
                artifact_set=getattr(payload, "artifact_set_sha256", None)
                == artifact_set_sha256,
                plan=existing.plan_digest == plan_digest,
            )
        else:
            matches["operator_action"] = payload.operator_action == "download-model"
        if not all(matches.values()):
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another cache operation",
            )
        return cache._operation_view(existing)

    def _resolve_requested_manifest(
        self,
        *,
        artifact_set_sha256: str | None,
        model_content_sha256: str | None,
        recipe_revision_sha256: str | None,
        recipe_revision_id: str | None,
        artifacts: Sequence[object] | None,
    ) -> ArtifactSetManifest:
        cache = cast("ModelCacheService", self)
        requested_set = _optional_digest(artifact_set_sha256)
        if (
            requested_set is not None
            and artifacts is None
            and (
                model_content_sha256 is None
                and recipe_revision_sha256 is None
                and recipe_revision_id is None
            )
        ):
            return cache._manifest_for_set(requested_set)
        manifest = cache.resolve_artifact_set(
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        if requested_set is not None and manifest.digest != requested_set:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.PIN_MISMATCH,
                "requested artifact-set identity does not match the resolved pins",
            )
        return manifest

    def download_preview(
        self,
        *,
        artifact_set_sha256: str | None = None,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[object] | None = None,
    ) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        manifest = cache._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        return cache._download_preview_for_manifest(manifest)

    def _download_preview_for_manifest(
        self, manifest: ArtifactSetManifest
    ) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        cached = cache._managed_cached_objects(manifest)
        already_cached = sum(
            spec.expected_bytes
            for digest, spec in _unique_artifacts(manifest.artifacts).items()
            if digest in cached
        )
        transfer = cache._transfer_state_for_manifest(
            manifest, force=False, cached=cached
        )
        new_bytes = transfer.total_bytes
        # A split file is assembled part by part, each part deleted once
        # appended: the disk peaks at the file plus its largest part.
        needed = new_bytes + _split_transient_bytes(manifest, cached)
        blockers = []
        if needed > cache.free_bytes():
            blockers.append(ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE)
            cache._request_storage(
                needed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
            )
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "download",
            "artifact_set_sha256": manifest.digest,
            "manifest": manifest.document(),
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "source_policy": SOURCE_POLICY,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": manifest.digest,
            "plan_digest": _sha256_json(plan),
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(manifest.artifacts),
            "expected_bytes": manifest.expected_bytes,
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "blockers": blockers,
            "warnings": [],
            "_manifest": manifest,
            "_transfer": transfer,
        }
