"""Repair."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from sqlalchemy import func, select
from vonk_agent_protocol import ModelCacheBlockerCode, ModelCacheCode, input_state

from .. import model_cache_states
from ..bounded_json import require_integer, require_mapping, require_sequence
from ..categorized_errors import InvalidValue
from ..lifecycle import Outcome, Reported
from ..lifecycle.model_cache import ModelCacheAdapter
from ..model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheOperationPayload,
    ModelCacheRepairCheckpoint,
    ModelCacheRepairPayload,
    ModelCacheRetry,
)
from ..model_cache_progress import progress_document
from ..models import ModelCacheOperation, ModelCacheSet
from ..operation_contract import AvailabilityOperationFailure
from ..strict_json import serialize_json_value
from .artifacts import (
    ArtifactSetManifest,
    _optional_digest,
    _sha256_json,
    _split_transient_bytes,
    _unique_artifacts,
)
from .catalog_helpers import _datetime, _iso
from .constants import _RETRY_BASE_SECONDS, SCHEMA_VERSION, SOURCE_POLICY
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheError,
    ModelCacheNotFoundInvalid,
)
from .persistence import _cache_failure, _manifest_of, _write_operation_payload
from .source_helpers import _is_hf_authority, _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class RepairMixin:
    """Repair behavior of the cache service."""

    @staticmethod
    def _payload_has_huggingface_source(payload: ModelCacheOperationPayload) -> bool:
        if not isinstance(payload, ModelCacheDownloadPayload):
            return False
        for artifact in payload.manifest.artifacts:
            source = artifact.source
            try:
                host = urlsplit(source).hostname
            except ValueError:
                host = None
            if _is_hf_authority(host):
                return True
        return False

    @classmethod
    def _manifest_has_huggingface_source(cls, manifest: ArtifactSetManifest) -> bool:
        for spec in _unique_artifacts(manifest.artifacts).values():
            try:
                host = urlsplit(spec.source).hostname
            except ValueError:
                continue
            if _is_hf_authority(host):
                return True
        return False

    def _record_huggingface_cooldown(self, until: datetime) -> None:
        cache = cast("ModelCacheService", self)
        with cache._lock:
            if cache._hf_cooldown_until is None or until > cache._hf_cooldown_until:
                cache._hf_cooldown_until = until

    def _refresh_huggingface_cooldown(
        self, rows: Sequence[ModelCacheOperation], now: datetime
    ) -> None:
        """Reconstruct HF throttling after restart from durable failure rows."""
        cache = cast("ModelCacheService", self)

        latest = cache._hf_cooldown_until
        for operation in rows:
            payload = cache._payload_or_none(operation)
            if payload is None or not cache._payload_has_huggingface_source(payload):
                continue  # unreadable: it carries no usable throttle evidence
            failure = cache._canonical_failure(operation)
            if failure is None or failure.code != "rate_limited":
                continue
            retry_at = failure.retry_time
            if retry_at is None:
                continue
            candidate = _datetime(datetime.fromisoformat(retry_at))
            if candidate > now and (latest is None or candidate > latest):
                latest = candidate
        cache._hf_cooldown_until = (
            latest if latest is not None and latest > now else None
        )

    def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = cache.get_entry(digest)
        artifact_rows = require_sequence(entry["artifacts"], "artifacts")
        artifact_digests = [
            require_mapping(item, "cache artifact entry")["sha256"]
            for item in artifact_rows
        ]
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "repair",
            "artifact_set_sha256": digest,
            "artifacts": artifact_digests,
            "source_policy": SOURCE_POLICY,
        }
        plan_digest = _sha256_json(plan)
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": digest,
            "plan_digest": plan_digest,
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(artifact_rows),
            "current_state": entry["state"],
            "expected_bytes": entry["expected_bytes"],
            "verified_bytes": entry["verified_bytes"],
        }

    def start_repair(
        self,
        *,
        actor: str,
        request_key: str,
        artifact_set_sha256: str,
        plan_digest: str,
    ) -> CacheOperationView:
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert digest is not None and requested_plan is not None
        preview = cache.repair_preview(digest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.STALE_PLAN, "repair preview is stale"
            )
        request_key = _request_key(request_key)
        with cache._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                return cache._operation_view(existing)
        manifest = cache._manifest_for_set(digest)
        transfer = cache._transfer_state_for_manifest(manifest, force=True)
        repair_bytes = transfer.total_bytes + _split_transient_bytes(manifest, None)
        waiting_for = ""
        if repair_bytes > cache.free_bytes():
            cache._request_storage(
                repair_bytes, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
            )
            waiting_for = ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
        capacity = cache._capacity_wait(waiting_for)
        wait_until = None if capacity is None else capacity[1]
        payload = _write_operation_payload(
            "repair",
            ModelCacheRepairPayload(
                repair_checkpoint=ModelCacheRepairCheckpoint(
                    transfer_id=uuid.uuid4().hex, completed_objects=[]
                ),
                schema_version=SCHEMA_VERSION,
                source_policy=SOURCE_POLICY,
                artifact_set_sha256=digest,
                manifest=manifest.contract(),
                plan_digest=requested_plan,
                transfer=transfer,
                retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                failure=None if capacity is None else capacity[0],
            ),
        )
        with cache._lock, cache._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                now = cache._clock()
                cache._require_model_sets_open(session, (digest,), now=now)
                operation = ModelCacheAdapter.new_operation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="repair",
                    next_action_at=wait_until,
                    attempt=1,
                    artifact_set_sha256=digest,
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
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return cache.get_operation(operation_id)

    def _capacity_wait(
        self, waiting_for: str
    ) -> tuple[AvailabilityOperationFailure, datetime] | None:
        """Make a capacity blocker a wait on the new operation, not a refusal.

        The storage demand is already filed (the retention sweep frees space);
        the operation is queued with a retryable ``download_blocked`` failure and
        the shared retry clock.  The claim loop re-checks the space when the
        clock is due (:meth:`_capacity_holds`), so nothing is downloaded into a
        full disk and nothing is left for a person to resubmit.
        """
        cache = cast("ModelCacheService", self)

        if not waiting_for:
            return None
        due = cache._clock() + timedelta(seconds=_RETRY_BASE_SECONDS)
        failure = _cache_failure(
            ModelCacheCode.DOWNLOAD_BLOCKED,
            waiting_for,
            retryable=True,
            recovery="capacity",
            retry_time=_iso(due),
            retry_after_seconds=_RETRY_BASE_SECONDS,
        )
        return failure, due

    def _capacity_holds(
        self,
        operation: ModelCacheOperation,
        payload: ModelCacheDownloadPayload,
        now: datetime,
    ) -> bool:
        """Whether a capacity-blocked operation still lacks the space it needs.

        If so the core reschedules it (bounded backoff, never an operator wait).
        """
        cache = cast("ModelCacheService", self)

        failure = payload.failure
        if failure is None or failure.code not in {
            ModelCacheCode.DOWNLOAD_BLOCKED,
            ModelCacheCode.CAPACITY,
        }:
            return False
        try:
            needed = payload.transfer.total_bytes + _split_transient_bytes(
                _manifest_of(payload), None
            )
        except (TypeError, ValueError, ModelCacheError):
            return False
        if needed <= cache.free_bytes():
            return False
        cache._request_storage(
            needed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
        )
        cache._lifecycle.complete(
            operation,
            Reported(
                Outcome.UNKNOWN,
                retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                reason=ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE,
            ),
            now,
            interrupted=False,
            consume_retry=False,
        )
        return True

    def _manifest_for_set(self, digest: str) -> ArtifactSetManifest:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            row = session.get(ModelCacheSet, digest)
            # The row's key is the identity assigned at ingress (verified there,
            # never re-hashed inside the system); a stored manifest that does not
            # read is re-derived from the catalog, else the set is unknown.
            manifest = None if row is None else cache._stored_manifest(row)
            if manifest is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.ENTRY_MISSING, "cache entry was not found"
                )
            return manifest

    def manifest_for_artifact_set(
        self, artifact_set_sha256: str
    ) -> ArtifactSetManifest:
        """Return the persisted immutable manifest for an exact artifact set.

        Consumers that prepare or distribute a model use this boundary rather
        than rebuilding an identity from display metadata.  The persisted
        manifest is re-hashed before it is returned, so a database row with a
        mismatched primary key cannot become a trusted source descriptor.
        """
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        return cache._manifest_for_set(digest)

    def preparation_evidence(self, artifact_set_sha256: str) -> dict[str, object]:
        """Project exact model preparation evidence for run/profile adapters.

        The cache owns Controller-side model bytes only.  ``targets`` stays
        empty because target readiness is established by the distribution
        worker after agent-authenticated transfer and verification.
        """
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = cache.get_entry(digest)
        manifest = cache.manifest_for_artifact_set(digest)
        dependencies = sorted(
            value
            for value in manifest.model_content_digests
            if value != manifest.model_content_sha256
        )
        expected_bytes = require_integer(entry["expected_bytes"], "expected bytes")
        verified_bytes = require_integer(entry["verified_bytes"], "verified bytes")
        complete = entry["coverage"] == "complete"
        state = str(entry["state"])
        controller_state = {
            "cached": "ready",
            "incomplete": "preparing",
            "downloading": "preparing",
            "verifying": "verifying",
            "needs-repair": "failed",
            "failed": "failed",
        }.get(state, "unknown")
        reason = None
        if manifest.model_content_sha256 is None:
            # No primary model pin: the evidence is unknown, not a refusal.
            controller_state = "unknown"
            reason = "the artifact set pins no primary model definition"
        elif controller_state in {"failed", "unknown"}:
            reason = str(entry.get("last_error") or "model cache is not complete")
        return {
            "artifact_set_sha256": digest,
            "model_content_sha256": manifest.model_content_sha256,
            "recipe_revision_sha256": manifest.recipe_revision_sha256,
            "artifact_count": len(manifest.artifacts),
            "artifact_set_bytes": expected_bytes,
            "dependency_model_content_sha256": dependencies,
            "completeness": "complete" if complete else "incomplete",
            "controller": {
                "state": controller_state,
                "expected_bytes": expected_bytes,
                "verified_bytes": verified_bytes,
                "missing_bytes": max(0, expected_bytes - verified_bytes),
                "verified_sha256": digest if complete else None,
                "verified_at": entry.get("verified_at"),
                "source": "nas-cache",
                "reason": reason,
            },
            "targets": [],
        }

    def activity_operations(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 101,
        state: str | None = None,
        node_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Return cache operations for the global Activity provider seam.

        Cache work is Controller/NAS scoped and therefore has no Spark node
        IDs.  A node filter consequently returns an empty page while still
        reporting the unfiltered-by-cursor total for the requested state.
        ``after`` is the already authenticated global activity boundary.
        """
        cache = cast("ModelCacheService", self)
        from ..operation_api import _activity_keyset_filter

        if not 1 <= limit <= 101:
            raise InvalidValue("operation provider page limit is invalid")
        if state is not None and (not isinstance(state, str) or not state.strip()):
            raise InvalidValue("operation state filter is invalid")
        if node_id is not None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        # A filter may still name a retired spelling (one release).
        named = None if state is None else input_state(state)
        if state is not None and named is None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        with cache._session() as session:
            filters = []
            if named is not None:
                filters.append(
                    ModelCacheOperation.state.in_(model_cache_states.words(named))
                )
            if request_id is not None:
                filters.append(ModelCacheOperation.request_key == request_id)
            boundary = None if after is None else (_datetime(after[0]), after[1])
            keyset = _activity_keyset_filter(
                ModelCacheOperation.created_at, ModelCacheOperation.id, "", boundary
            )
            if keyset is not None:
                filters.append(keyset)
            rows = list(
                session.scalars(
                    select(ModelCacheOperation)
                    .where(*filters)
                    .order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                    # Fetch one sentinel row so the provider can expose a
                    # stable boundary instead of silently truncating pages.
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            total_filters = []
            if state is not None:
                total_filters.append(ModelCacheOperation.state == state)
            if request_id is not None:
                total_filters.append(ModelCacheOperation.request_key == request_id)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(*total_filters)
                )
                or 0
            )
        next_boundary = None
        if has_more and rows:
            last = rows[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "operations": tuple(cache._operation_view(row) for row in rows),
            "total": total,
            "_next_boundary": next_boundary,
        }
