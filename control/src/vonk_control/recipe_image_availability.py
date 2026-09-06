"""Durable preparation of one exact canonical Recipe's runtime image.

The availability operation is deliberately separate from Run/Switch.  It
refreshes catalog metadata before taking a snapshot of the selected Recipe,
then prepares that snapshot without changing a pin or a running workload.  A
``Job`` row is used as the restart-safe operation record so the worker and the
API can observe the same status without a second operation database.

This module owns image preparation only.  Model file transfers remain owned by
``model_cache`` and can be linked by the caller through ``model_digest``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .models import CatalogDocumentRevision, Job, RecipeBuild
from .operation_contract import normalize_operation_progress, sanitize_failure_evidence
from .runtime_image_preparation import (
    RuntimeImageReceipt,
    RuntimeImageStorage,
    prepare_runtime_image,
    persist_runtime_image_receipt,
)

SCHEMA_VERSION = 2
OPERATION_KIND = "recipe.image.availability.v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_AUTOMATIC_ATTEMPTS = 3
_MAX_OPERATOR_RETRIES = 3
_TERMINAL_FAILURE_CODES = frozenset(
    {
        "recipe_image.identity_conflict",
        "recipe_image.metadata_stale",
        "recipe_image.recipe_invalid",
        "recipe_image.recipe_unavailable",
        "recipe_image.runtime_invalid",
        "runtime_image.digest_mismatch",
        "runtime_image.archive_mismatch",
        "runtime_image.archive_conflict",
        "runtime_image.receipt_identity_conflict",
        "runtime_image.authorization_invalid",
    }
)
_CAPACITY_FAILURE_CODES = frozenset(
    {
        "build.insufficient_disk",
        "build.insufficient_memory",
        "recipe_image.insufficient_disk",
        "recipe_image.insufficient_memory",
        "runtime_image.insufficient_disk",
    }
)
_INTEGRITY_FAILURE_CODES = frozenset(
    {
        "registry.digest_mismatch",
        "recipe_package.digest_mismatch",
        "runtime_image.digest_mismatch",
        "runtime_image.archive_mismatch",
        "runtime_image.archive_conflict",
        "runtime_image.evidence_invalid",
    }
)


class RecipeImageAvailabilityError(RuntimeError):
    """A bounded operator-facing availability failure."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        recovery_actions: Sequence[str] = (),
        log_excerpt: str | None = None,
        step: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.recovery_actions = tuple(recovery_actions)
        self.log_excerpt = log_excerpt
        self.step = step
        super().__init__(detail)


class RecipeAuthorityResolver(Protocol):
    """Refresh and resolve the selected canonical Recipe in one operation."""

    def __call__(
        self, recipe_revision_id: str
    ) -> tuple[RecipeDefinition | Mapping[str, object], Mapping[str, object]]: ...


class RecipeImageBuilder(Protocol):
    """Build the exact source recipe and report bounded progress."""

    def __call__(
        self,
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
        *,
        build_input_sha256: str,
        force: bool,
        progress: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityView:
    id: str
    request_id: str
    kind: str
    state: str
    attempt: int
    recipe_revision_id: str
    recipe_content_sha256: str
    model_digest: str | None
    build_input_sha256: str | None
    progress: Mapping[str, object]
    result: Mapping[str, object] | None
    failure: Mapping[str, object] | None
    supported_actions: tuple[str, ...]
    created_at: str
    updated_at: str

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "request_id": self.request_id,
            "kind": self.kind,
            "state": self.state,
            "attempt": self.attempt,
            "recipe_revision_id": self.recipe_revision_id,
            "recipe_content_sha256": self.recipe_content_sha256,
            "model_digest": self.model_digest,
            "build_input_sha256": self.build_input_sha256,
            "progress": dict(self.progress),
            "result": None if self.result is None else dict(self.result),
            "failure": None if self.failure is None else dict(self.failure),
            "supported_actions": list(self.supported_actions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityClaim:
    """Small scheduler hook returned for execution outside the worker tick."""

    operation_id: str
    recipe_revision_id: str
    image_identity: str | None
    build_input_sha256: str | None
    claim_owner: str


def _iso(value: datetime) -> str:
    value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RecipeImageAvailabilityError(
            "recipe_image.identity_invalid", f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field=field)


def _canonical_recipe(value: RecipeDefinition | Mapping[str, object]) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    try:
        return RecipeDefinition.model_validate(value)
    except Exception as error:
        raise RecipeImageAvailabilityError(
            "recipe_image.recipe_invalid", "selected recipe is not a canonical RecipeDefinition"
        ) from error


def _image_identity(recipe: RecipeDefinition) -> str | None:
    if recipe.execution.mode != "image" or recipe.execution.image is None:
        return None
    return f"sha256:{recipe.execution.image.digest}"


def _known_total(runtime: Mapping[str, object]) -> int | None:
    for key in ("image_bytes", "expected_bytes", "total_bytes"):
        value = runtime.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _progress(
    phase: str,
    *,
    completed_bytes: int = 0,
    total_bytes: int | None = None,
    bytes_per_second: float | None = None,
    eta_seconds: float | None = None,
    checkpoint: Mapping[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "phase": phase,
        "completed_bytes": max(0, completed_bytes),
        "total_bytes_known": total_bytes is not None,
    }
    if total_bytes is not None:
        value["total_bytes"] = total_bytes
    if bytes_per_second is not None:
        value["bytes_per_second"] = bytes_per_second
    if eta_seconds is not None:
        value["eta_seconds"] = eta_seconds
    if checkpoint is not None:
        value["checkpoint"] = dict(checkpoint)
    return normalize_operation_progress(value)


def _retryable(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _TERMINAL_FAILURE_CODES:
        return False
    if getattr(error, "retryable", False) is True:
        return True
    status = getattr(error, "status_code", None)
    if type(status) is int:
        return status == 429 or status >= 500
    text = f"{getattr(error, 'code', '')} {getattr(error, 'detail', str(error))}".casefold()
    return isinstance(error, (OSError, TimeoutError, ConnectionError)) or any(
        marker in text for marker in ("timeout", "timed out", "connection", "network", "transport", "temporarily", "copy")
    )


def _retry_after(error: BaseException) -> int | None:
    value = getattr(error, "retry_after_seconds", None)
    if type(value) is int and 0 <= value <= 86_400:
        return value
    return None


def _log_excerpt(error: BaseException) -> str | None:
    value = getattr(error, "log_excerpt", None)
    if value is None:
        value = getattr(error, "detail", None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:1024]


def _recovery_actions(
    payload: Mapping[str, object], code: str, retryable: bool
) -> list[str]:
    """Map stable failure classes to UI action identifiers."""

    mode = payload.get("execution_mode")
    if code in _CAPACITY_FAILURE_CODES:
        return ["free_space"]
    if code in _INTEGRITY_FAILURE_CODES:
        return ["download_again"] if mode == "image" else ["force_rebuild"]
    if retryable:
        resumable = mode == "image" and (
            code.startswith("registry.")
            or code.startswith("runtime_image.transport")
            or code in {"recipe_image.download_interrupted", "recipe_image.network_error"}
        )
        return ["resume", "retry"] if resumable else ["retry"]
    return ["inspect"]


class RecipeImageAvailabilityService:
    """Persist and execute exact recipe-image availability operations.

    ``authority`` must perform the latest metadata refresh and return the
    selected revision's canonical recipe plus its compiled runtime projection.
    ``builder`` is called only for source-build recipes; direct-image recipes
    use :func:`prepare_runtime_image` and the existing OCI transport.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        storage: RuntimeImageStorage,
        authority: RecipeAuthorityResolver,
        transport: object | None = None,
        builder: RecipeImageBuilder | None = None,
        clock: Callable[[], datetime],
        receipt_writer: Callable[[Session, str, str, str, RuntimeImageReceipt], object]
        | None = None,
        automatic_attempt_limit: int = _MAX_AUTOMATIC_ATTEMPTS,
        operator_retry_limit: int = _MAX_OPERATOR_RETRIES,
        max_parallel: int = 4,
        max_parallel_builds: int = 1,
        builder_admission: Callable[[RecipeDefinition, Mapping[str, object]], None] | None = None,
        claim_lease_seconds: int = 120,
    ) -> None:
        if not 1 <= automatic_attempt_limit <= 8:
            raise ValueError("automatic attempt limit is invalid")
        if not 0 <= operator_retry_limit <= 8:
            raise ValueError("operator retry limit is invalid")
        if not 1 <= max_parallel <= 16:
            raise ValueError("availability parallelism is invalid")
        if not 1 <= max_parallel_builds <= max_parallel:
            raise ValueError("availability build parallelism is invalid")
        if not 10 <= claim_lease_seconds <= 3_600:
            raise ValueError("availability claim lease is invalid")
        self._sessions = sessions
        self._storage = storage
        self._authority = authority
        self._transport = transport
        self._builder = builder
        self._clock = clock
        self._receipt_writer = receipt_writer
        self._automatic_attempt_limit = automatic_attempt_limit
        self._operator_retry_limit = operator_retry_limit
        self._max_parallel = max_parallel
        self._max_parallel_builds = max_parallel_builds
        self._builder_admission = builder_admission
        self._claim_lease_seconds = claim_lease_seconds
        self._identity_locks: dict[str, threading.Lock] = {}
        self._identity_locks_guard = threading.Lock()

    def start(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        request_id: str,
        model_digest: str | None = None,
        build_input_sha256: str | None = None,
        effective_execution_key: str | None = None,
        force: bool = False,
        force_download: bool = False,
        force_rebuild: bool = False,
    ) -> RecipeImageAvailabilityView:
        """Refresh metadata and queue one exact selected Recipe operation."""

        if not isinstance(recipe_revision_id, str) or not recipe_revision_id.strip():
            raise RecipeImageAvailabilityError("recipe_image.recipe_invalid", "recipe revision is required")
        if force and (force_download or force_rebuild):
            raise RecipeImageAvailabilityError(
                "recipe_image.action_invalid", "force cannot be combined with an explicit image action"
            )
        if force_download and force_rebuild:
            raise RecipeImageAvailabilityError(
                "recipe_image.action_invalid", "download again and rebuild are mutually exclusive"
            )
        model_digest = _optional_digest(model_digest, field="model_digest")
        build_input_sha256 = _optional_digest(build_input_sha256, field="build_input_sha256")
        effective_execution_key = _optional_digest(
            effective_execution_key, field="effective_execution_key"
        )
        existing = self._request_replay(
            request_id,
            recipe_revision_id=recipe_revision_id,
            force=force or force_download or force_rebuild,
            model_digest=model_digest,
            build_input_sha256=build_input_sha256,
            effective_execution_key=effective_execution_key,
        )
        if existing is not None:
            return existing
        if self._authority is None:
            raise RecipeImageAvailabilityError(
                "recipe_image.metadata_refresh_unavailable", "latest recipe metadata could not be refreshed"
            )
        try:
            raw_recipe, runtime = self._authority(recipe_revision_id)
        except RecipeImageAvailabilityError:
            raise
        except Exception as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.metadata_refresh_failed",
                "latest recipe metadata could not be refreshed",
                retryable=_retryable(error),
                retry_after_seconds=_retry_after(error),
                recovery_actions=("retry",) if _retryable(error) else ("inspect",),
            ) from error
        recipe = _canonical_recipe(raw_recipe)
        computed_digest = content_sha256(recipe)
        if not isinstance(runtime, Mapping):
            raise RecipeImageAvailabilityError(
                "recipe_image.runtime_invalid", "selected recipe runtime projection is unavailable"
            )
        with self._sessions.begin() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None or revision.kind != "recipe" or revision.state != "active":
                raise RecipeImageAvailabilityError(
                    "recipe_image.recipe_unavailable", "selected recipe revision is unavailable or inactive"
                )
            if revision.content_digest != computed_digest:
                raise RecipeImageAvailabilityError(
                    "recipe_image.metadata_stale", "refreshed recipe does not match the selected revision"
                )
            if effective_execution_key is None:
                effective_execution_key = revision.execution_key
            if effective_execution_key != revision.execution_key:
                raise RecipeImageAvailabilityError(
                    "recipe_image.identity_conflict", "selected recipe execution identity changed"
                )
            if recipe.execution.mode == "image" and force_rebuild:
                raise RecipeImageAvailabilityError(
                    "recipe_image.action_invalid", "rebuild is supported only for source-build recipes"
                )
            if recipe.execution.mode == "build" and force_download:
                raise RecipeImageAvailabilityError(
                    "recipe_image.action_invalid", "download again is supported only for published images"
                )
            if force:
                if recipe.execution.mode == "image":
                    force_download = True
                else:
                    force_rebuild = True
            runtime_build_input = runtime.get("build_input_sha256")
            if recipe.execution.mode == "build":
                if not isinstance(runtime_build_input, str):
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_input_missing",
                        "authoritative runtime projection lacks the exact build input digest",
                    )
                runtime_build_input = _digest(runtime_build_input, field="build_input_sha256")
                if build_input_sha256 is None:
                    build_input_sha256 = runtime_build_input
                elif build_input_sha256 != runtime_build_input:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.identity_conflict",
                        "submitted build input does not match authoritative runtime metadata",
                    )
            else:
                build_input_sha256 = None
            image_identity = _image_identity(recipe)
            identity_key = image_identity or build_input_sha256
            payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "kind": OPERATION_KIND,
                "recipe_revision_id": recipe_revision_id,
                "recipe_content_sha256": computed_digest,
                "effective_execution_key": effective_execution_key,
                "model_digest": model_digest,
                "build_input_sha256": build_input_sha256,
                "image_identity": image_identity,
                "identity_key": identity_key,
                "execution_mode": recipe.execution.mode,
                "recipe": recipe.model_dump(mode="json"),
                "runtime": dict(runtime),
                "force_download": force_download,
                "force_rebuild": force_rebuild,
                "progress": _progress("prepare", total_bytes=_known_total(runtime)),
                "retry": {"automatic_attempts": 0, "operator_retries": 0},
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                existing_payload = existing.payload if isinstance(existing.payload, Mapping) else {}
                existing_force = bool(
                    existing_payload.get("force_download") is True
                    or existing_payload.get("force_rebuild") is True
                )
                if (
                    existing.kind != OPERATION_KIND
                    or existing_payload.get("recipe_revision_id") != recipe_revision_id
                    or existing_force != bool(force_download or force_rebuild)
                ):
                    raise RecipeImageAvailabilityError(
                        "recipe_image.request_key_reused", "request key was already used for another operation"
                    )
                return self._view(existing)
            now = self._clock()
            operation = Job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=OPERATION_KIND,
                state="queued",
                actor=actor,
                authority_revision=recipe_revision_id,
                targets=[recipe_revision_id],
                payload_digest=hashlib.sha256(encoded).hexdigest(),
                payload=payload,
                result=None,
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return self._view(operation)

    def _request_replay(
        self,
        request_id: str,
        *,
        recipe_revision_id: str,
        force: bool,
        model_digest: str | None,
        build_input_sha256: str | None,
        effective_execution_key: str | None,
    ) -> RecipeImageAvailabilityView | None:
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is None:
                return None
            payload = existing.payload if isinstance(existing.payload, Mapping) else {}
            existing_force = bool(
                payload.get("force_download") is True
                or payload.get("force_rebuild") is True
            )
            if (
                existing.kind != OPERATION_KIND
                or payload.get("recipe_revision_id") != recipe_revision_id
                or existing_force != force
                or (
                    model_digest is not None
                    and payload.get("model_digest") != model_digest
                )
                or (
                    build_input_sha256 is not None
                    and payload.get("build_input_sha256") != build_input_sha256
                )
                or (
                    effective_execution_key is not None
                    and payload.get("effective_execution_key") != effective_execution_key
                )
            ):
                raise RecipeImageAvailabilityError(
                    "recipe_image.request_key_reused",
                    "request key was already used for another operation",
                )
            return self._view(existing)

    def get(self, operation_id: str) -> RecipeImageAvailabilityView:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None or operation.kind != OPERATION_KIND:
                raise KeyError(operation_id)
            return self._view(operation)

    def retry(self, operation_id: str, *, actor: str, request_id: str) -> RecipeImageAvailabilityView:
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None or previous.kind != OPERATION_KIND:
                raise KeyError(operation_id)
            if previous.state != "failed":
                raise RecipeImageAvailabilityError("recipe_image.not_retryable", "operation is not failed")
            previous_payload = previous.payload if isinstance(previous.payload, Mapping) else {}
            failure = previous_payload.get("failure", {})
            failure = failure if isinstance(failure, Mapping) else {}
            if failure.get("retryable") is not True:
                raise RecipeImageAvailabilityError("recipe_image.not_retryable", "operation failure is terminal")
            retry = previous.payload.get("retry", {})
            retry_count = int(retry.get("operator_retries", 0)) if isinstance(retry, Mapping) else 0
            if retry_count >= self._operator_retry_limit:
                raise RecipeImageAvailabilityError("recipe_image.retry_exhausted", "operator retry limit reached")
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._view(existing)
            payload = dict(previous.payload)
            payload["retry"] = {"automatic_attempts": 0, "operator_retries": retry_count + 1}
            payload.pop("retry_after_at", None)
            payload.pop("failure", None)
            now = self._clock()
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            operation = Job(
                id=str(uuid.uuid4()), request_id=request_id, kind=OPERATION_KIND,
                state="queued", actor=actor, authority_revision=previous.authority_revision,
                targets=list(previous.targets), payload_digest=hashlib.sha256(encoded).hexdigest(),
                payload=payload, result=None, current_attempt=0, created_at=now, updated_at=now,
            )
            session.add(operation)
            session.flush()
            return self._view(operation)

    def resume_operations(self, *, limit: int = 16) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("availability operation limit is invalid")
        with self._sessions() as session:
            # SQLAlchemy's scalar count expression is portable across the
            # SQLite fixtures and PostgreSQL deployment.
            count = int(session.scalar(select(func.count()).select_from(Job).where(
                Job.kind == OPERATION_KIND,
                Job.state.in_(("queued", "running", "partial")),
            )) or 0)
            return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        if not 1 <= limit <= 16:
            raise ValueError("availability worker batch limit is invalid")
        claims = self.claim_pending(limit=min(limit, self._max_parallel))
        for claim in claims:
            self.run_claim(claim)
        return len(claims)

    def claim_pending(self, *, limit: int = 4, owner_id: str | None = None) -> tuple[RecipeImageAvailabilityClaim, ...]:
        """Return independent durable claims for an external worker scheduler.

        The caller may dispatch each claim on its own bounded executor.  The
        claims carry no network handle, so a process restart can safely find
        the same rows through :meth:`resume_operations`.
        """

        if not 1 <= limit <= self._max_parallel:
            raise ValueError("availability claim limit is invalid")
        owner_id = owner_id or str(uuid.uuid4())
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        lease_until = _iso(now + timedelta(seconds=self._claim_lease_seconds))
        claims: list[RecipeImageAvailabilityClaim] = []
        with self._sessions.begin() as session:
            rows = list(session.scalars(
                select(Job).where(
                    Job.kind == OPERATION_KIND,
                    Job.state.in_(("queued", "running", "partial")),
                ).order_by(Job.updated_at, Job.id).limit(limit * 8).with_for_update(skip_locked=True)
            ))
            active_builds = 0
            active_pulls = 0
            for active in rows:
                active_payload = active.payload if isinstance(active.payload, Mapping) else {}
                if active.state != "running":
                    continue
                active_until = active_payload.get("claim_until")
                if isinstance(active_until, str):
                    try:
                        parsed_until = datetime.fromisoformat(active_until)
                        parsed_until = parsed_until if parsed_until.tzinfo is not None else parsed_until.replace(tzinfo=UTC)
                        if now >= parsed_until:
                            continue
                    except ValueError:
                        pass
                if active_payload.get("execution_mode") == "build":
                    active_builds += 1
                else:
                    active_pulls += 1
            for operation in rows:
                payload = operation.payload if isinstance(operation.payload, Mapping) else {}
                if not self._retry_due(payload, now):
                    continue
                if operation.state == "running":
                    claimed_until = payload.get("claim_until")
                    if isinstance(claimed_until, str):
                        try:
                            parsed_until = datetime.fromisoformat(claimed_until)
                            parsed_until = parsed_until if parsed_until.tzinfo is not None else parsed_until.replace(tzinfo=UTC)
                            if now < parsed_until:
                                continue
                        except ValueError:
                            pass
                mode = payload.get("execution_mode")
                if mode == "build" and active_builds >= self._max_parallel_builds:
                    continue
                if mode != "build" and active_pulls >= self._max_parallel:
                    continue
                operation.state = "running"
                operation.current_attempt = int(operation.current_attempt) + 1
                operation.updated_at = now
                operation.payload = dict(payload) | {
                    "claim_owner": owner_id,
                    "claim_until": lease_until,
                }
                claims.append(
                    RecipeImageAvailabilityClaim(
                        operation_id=operation.id,
                        recipe_revision_id=str(payload.get("recipe_revision_id", "")),
                        image_identity=(
                            str(payload.get("image_identity"))
                            if payload.get("image_identity") is not None
                            else None
                        ),
                        build_input_sha256=(
                            str(payload.get("build_input_sha256"))
                            if payload.get("build_input_sha256") is not None
                            else None
                        ),
                        claim_owner=owner_id,
                    )
                )
                if mode == "build":
                    active_builds += 1
                else:
                    active_pulls += 1
                if len(claims) >= limit:
                    break
        return tuple(claims)

    def run_claim(self, claim: RecipeImageAvailabilityClaim) -> None:
        """Execute one claim; callers may run claims in their own bounded pool."""

        self._run(claim.operation_id, owner_id=claim.claim_owner)

    def _identity_lock(self, identity_key: str | None) -> threading.Lock:
        if not identity_key:
            return threading.Lock()
        with self._identity_locks_guard:
            lock = self._identity_locks.get(identity_key)
            if lock is None:
                lock = threading.Lock()
                self._identity_locks[identity_key] = lock
            return lock

    def _stored_build_receipt(self, build_input_sha256: str) -> Mapping[str, object] | None:
        with self._sessions() as session:
            build = session.scalar(
                select(RecipeBuild)
                .where(
                    RecipeBuild.build_input_sha256 == build_input_sha256,
                    RecipeBuild.state == "succeeded",
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
                .limit(1)
            )
            if (
                build is None
                or build.image_digest is None
                or build.oci_layout_sha256 is None
                or build.image_bytes is None
            ):
                return None
            return {
                "state": "succeeded",
                "build_id": build.id,
                "image_digest": build.image_digest,
                "oci_layout_sha256": build.oci_layout_sha256,
                "image_bytes": build.image_bytes,
            }

    def _eligible(self, operation_id: str) -> bool:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None or not isinstance(operation.payload, Mapping):
                return False
            value = operation.payload.get("retry_after_at")
            if not isinstance(value, str):
                return True
            try:
                eligible_at = datetime.fromisoformat(value)
            except ValueError:
                return True
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            eligible_at = eligible_at if eligible_at.tzinfo is not None else eligible_at.replace(tzinfo=UTC)
            return now >= eligible_at

    @staticmethod
    def _retry_due(payload: Mapping[str, object], now: datetime) -> bool:
        value = payload.get("retry_after_at")
        if not isinstance(value, str):
            return True
        try:
            eligible_at = datetime.fromisoformat(value)
        except ValueError:
            return True
        eligible_at = eligible_at if eligible_at.tzinfo is not None else eligible_at.replace(tzinfo=UTC)
        return now >= eligible_at

    def _run(self, operation_id: str, *, owner_id: str | None = None) -> None:
        with self._sessions.begin() as session:
            operation = session.get(Job, operation_id, with_for_update=True)
            if operation is None or operation.kind != OPERATION_KIND:
                return
            payload = dict(operation.payload)
            if owner_id is not None and payload.get("claim_owner") != owner_id:
                return
            was_running = operation.state == "running"
            operation.state = "running"
            if not was_running:
                operation.current_attempt = int(operation.current_attempt) + 1
            operation.updated_at = self._clock()
            self._set_progress(operation, "prepare", total_bytes=_known_total(payload.get("runtime", {})))
        heartbeat_stop = threading.Event()
        heartbeat = None
        if owner_id is not None:
            heartbeat = threading.Thread(
                target=self._renew_claim_loop,
                args=(operation_id, owner_id, heartbeat_stop),
                name=f"recipe-image-lease-{operation_id[:8]}",
                daemon=True,
            )
            heartbeat.start()
        try:
            recipe = _canonical_recipe(payload["recipe"])
            runtime = payload["runtime"]
            if not isinstance(runtime, Mapping):
                raise RecipeImageAvailabilityError("recipe_image.runtime_invalid", "runtime projection is invalid")
            identity_key = payload.get("identity_key")
            identity = identity_key if isinstance(identity_key, str) else None
            with self._identity_lock(identity):
                receipt = self._prepare_claimed_image(operation_id, payload, recipe, runtime)
            self._persist_receipt(operation_id, payload, receipt)
            result = {
                "schema_version": SCHEMA_VERSION,
                "recipe_content_sha256": payload["recipe_content_sha256"],
                "model_digest": payload.get("model_digest"),
                "build_input_sha256": payload.get("build_input_sha256"),
                "source": receipt.source,
                "registry_manifest_digest": receipt.registry_manifest_digest,
                "platform_manifest_digest": receipt.platform_manifest_digest,
                "image_digest": receipt.image_digest,
                "local_image_config_id": receipt.local_image_config_id,
                "oci_archive_sha256": receipt.oci_archive_sha256,
                "image_bytes": receipt.image_bytes,
                "build_id": receipt.build_id,
            }
            with self._sessions.begin() as session:
                operation = session.get(Job, operation_id)
                if operation is not None:
                    operation.state = "succeeded"
                    operation.result = result
                    operation.updated_at = self._clock()
                    operation.payload = dict(operation.payload) | {"stage": "available"}
                    operation.payload = dict(operation.payload) | {
                        "claim_owner": None,
                        "claim_until": None,
                    }
                    operation.current_attempt = int(operation.current_attempt)
                    self._set_progress(operation, "available", total_bytes=receipt.image_bytes,
                                       completed_bytes=receipt.image_bytes)
        except Exception as error:
            self._fail(operation_id, error)
        finally:
            if heartbeat is not None:
                heartbeat_stop.set()
                heartbeat.join(timeout=max(1.0, self._claim_lease_seconds / 2))

    def _prepare_claimed_image(
        self,
        operation_id: str,
        payload: Mapping[str, object],
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
    ) -> RuntimeImageReceipt:
        force_download = payload.get("force_download") is True
        force_rebuild = payload.get("force_rebuild") is True
        if recipe.execution.mode == "build":
            if self._builder is None:
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_unavailable", "no canonical recipe build executor is configured"
                )
            build_input_sha256 = payload.get("build_input_sha256")
            if not isinstance(build_input_sha256, str):
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_input_missing", "exact build input digest is missing"
                )
            if self._builder_admission is not None:
                self._builder_admission(recipe, runtime)
            self._update_progress(operation_id, "build", total_bytes=None)
            build_receipt = None if force_rebuild else self._stored_build_receipt(build_input_sha256)
            if build_receipt is None:
                def report(value: Mapping[str, object]) -> None:
                    self._update_progress(operation_id, "build", detail=value)

                build_receipt = self._builder(
                    recipe,
                    runtime,
                    build_input_sha256=build_input_sha256,
                    force=force_rebuild,
                    progress=report,
                )
            if not isinstance(build_receipt, Mapping):
                raise RecipeImageAvailabilityError("recipe_image.build_invalid", "builder returned no receipt")
            self._update_progress(operation_id, "verify")
            return prepare_runtime_image(
                recipe,
                runtime=runtime,
                storage=self._storage,
                transport=self._transport,
                build_receipt=build_receipt,
                now=self._clock(),
                force=False,
            )
        total = _known_total(runtime)
        self._update_progress(operation_id, "download", total_bytes=total)
        receipt = prepare_runtime_image(
            recipe,
            runtime=runtime,
            storage=self._storage,
            transport=self._transport,
            now=self._clock(),
            force=force_download,
        )
        self._update_progress(
            operation_id,
            "verify",
            total_bytes=receipt.image_bytes,
            completed_bytes=receipt.image_bytes,
        )
        return receipt

    def _renew_claim_loop(self, operation_id: str, owner_id: str, stop: threading.Event) -> None:
        interval = max(1.0, self._claim_lease_seconds / 3)
        while not stop.wait(interval):
            if not self._renew_claim(operation_id, owner_id):
                return

    def _renew_claim(self, operation_id: str, owner_id: str) -> bool:
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        with self._sessions.begin() as session:
            operation = session.get(Job, operation_id, with_for_update=True)
            if operation is None or operation.state != "running":
                return False
            payload = operation.payload if isinstance(operation.payload, Mapping) else {}
            if payload.get("claim_owner") != owner_id:
                return False
            operation.payload = dict(payload) | {
                "claim_until": _iso(now + timedelta(seconds=self._claim_lease_seconds)),
            }
            operation.updated_at = now
            return True

    def _persist_receipt(self, operation_id: str, payload: Mapping[str, object], receipt: RuntimeImageReceipt) -> None:
        execution_key = payload.get("effective_execution_key")
        if not isinstance(execution_key, str):
            raise RecipeImageAvailabilityError("recipe_image.identity_invalid", "effective execution identity is missing")
        with self._sessions.begin() as session:
            if self._receipt_writer is None:
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=str(payload["recipe_revision_id"]),
                    original_content_digest=str(payload["recipe_content_sha256"]),
                    effective_execution_key=execution_key,
                    receipt=receipt,
                    verified_at=self._clock(),
                )
            else:
                self._receipt_writer(
                    session, str(payload["recipe_revision_id"]), str(payload["recipe_content_sha256"]),
                    execution_key, receipt,
                )

    def _set_progress(self, operation: Job, phase: str, *, total_bytes: int | None = None,
                      completed_bytes: int = 0, bytes_per_second: float | None = None,
                      eta_seconds: float | None = None,
                      detail: Mapping[str, object] | None = None) -> None:
        progress = _progress(
            phase, total_bytes=total_bytes, completed_bytes=completed_bytes,
            bytes_per_second=bytes_per_second, eta_seconds=eta_seconds,
        )
        operation.payload = dict(operation.payload) | {"progress": progress}
        if detail:
            safe = sanitize_failure_evidence(detail)
            operation.payload = dict(operation.payload) | {
                "step": safe.get("step") or safe.get("current_step"),
                "log_excerpt": safe.get("log_excerpt") or safe.get("log"),
            }

    def _update_progress(self, operation_id: str, phase: str, *, total_bytes: int | None = None,
                         completed_bytes: int = 0, detail: Mapping[str, object] | None = None) -> None:
        with self._sessions.begin() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                return
            if detail is not None:
                raw_completed = detail.get("completed_bytes", detail.get("downloaded_bytes"))
                if type(raw_completed) is int and raw_completed >= 0:
                    completed_bytes = raw_completed
                raw_total = detail.get("total_bytes", detail.get("expected_bytes"))
                if type(raw_total) is int and raw_total >= 0:
                    total_bytes = raw_total
                raw_rate = detail.get("bytes_per_second", detail.get("rate"))
                bytes_per_second = (
                    float(raw_rate) if isinstance(raw_rate, (int, float)) and not isinstance(raw_rate, bool) and raw_rate >= 0 else None
                )
                raw_eta = detail.get("eta_seconds")
                eta_seconds = (
                    float(raw_eta) if isinstance(raw_eta, (int, float)) and not isinstance(raw_eta, bool) and raw_eta >= 0 else None
                )
            else:
                bytes_per_second = None
                eta_seconds = None
            raw_progress = operation.payload.get("progress") if isinstance(operation.payload, Mapping) else None
            old = raw_progress if isinstance(raw_progress, Mapping) else {}
            old_completed = old.get("completed_bytes", 0)
            if type(old_completed) is int and old_completed > completed_bytes:
                completed_bytes = old_completed
            self._set_progress(
                operation, phase, total_bytes=total_bytes, completed_bytes=completed_bytes,
                bytes_per_second=bytes_per_second, eta_seconds=eta_seconds, detail=detail,
            )
            operation.updated_at = self._clock()

    def _fail(self, operation_id: str, error: BaseException) -> None:
        retryable = _retryable(error)
        code = getattr(error, "code", type(error).__name__.lower())
        detail = getattr(error, "detail", str(error))
        step = getattr(error, "step", None)
        if isinstance(step, str) and step.strip():
            detail = f"{step.strip()}: {detail}"
        retry_after = _retry_after(error)
        excerpt = _log_excerpt(error)
        with self._sessions.begin() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                return
            retry = operation.payload.get("retry", {})
            retry = dict(retry) if isinstance(retry, Mapping) else {}
            automatic_attempts = int(retry.get("automatic_attempts", 0))
            if retryable and retry_after is None:
                retry_after = min(60, 2 ** automatic_attempts)
            bounded = retryable and automatic_attempts + 1 < self._automatic_attempt_limit
            retry["automatic_attempts"] = automatic_attempts + 1
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            required_bytes = getattr(error, "required_bytes", None)
            free_bytes = getattr(error, "free_bytes", None)
            shortfall_bytes = getattr(error, "shortfall_bytes", None)
            if required_bytes is None:
                required_bytes = getattr(error, "disk_required_bytes", None)
            if free_bytes is None:
                free_bytes = getattr(error, "disk_free_bytes", None)
            if shortfall_bytes is None and type(required_bytes) is int and type(free_bytes) is int:
                shortfall_bytes = max(0, required_bytes - free_bytes)
            failure: dict[str, object] = {
                "code": str(code)[:64],
                "detail": str(detail)[:512],
                "recovery_actions": _recovery_actions(operation.payload, str(code), retryable),
                "retryable": retryable,
                "retry_time": (
                    _iso(now + timedelta(seconds=retry_after))
                    if retry_after is not None
                    else None
                ),
                "retry_after_seconds": retry_after,
                "log_excerpt": excerpt,
                "required_bytes": required_bytes if type(required_bytes) is int and required_bytes >= 0 else None,
                "free_bytes": free_bytes if type(free_bytes) is int and free_bytes >= 0 else None,
                "shortfall_bytes": shortfall_bytes if type(shortfall_bytes) is int and shortfall_bytes >= 0 else None,
            }
            failure = sanitize_failure_evidence(failure)
            operation.result = None
            payload = dict(operation.payload) | {"retry": retry, "failure": failure}
            if retry_after is not None:
                payload["retry_after_at"] = _iso(now + timedelta(seconds=retry_after))
            else:
                payload.pop("retry_after_at", None)
            payload["claim_owner"] = None
            payload["claim_until"] = None
            operation.payload = payload
            operation.state = "queued" if bounded else "failed"
            operation.updated_at = self._clock()
            operation.current_attempt = int(operation.current_attempt)

    @staticmethod
    def _view(operation: Job) -> RecipeImageAvailabilityView:
        payload = operation.payload if isinstance(operation.payload, Mapping) else {}
        result = operation.result if isinstance(operation.result, Mapping) else None
        raw_failure = payload.get("failure")
        failure = raw_failure if isinstance(raw_failure, Mapping) else None
        actions = (
            tuple(str(item) for item in failure.get("recovery_actions", []) if isinstance(item, str))
            if failure is not None and isinstance(failure.get("recovery_actions"), list)
            else ()
        )
        return RecipeImageAvailabilityView(
            id=operation.id, request_id=operation.request_id, kind=operation.kind,
            state=operation.state, attempt=int(operation.current_attempt),
            recipe_revision_id=str(payload.get("recipe_revision_id", "")),
            recipe_content_sha256=str(payload.get("recipe_content_sha256", "")),
            model_digest=(payload.get("model_digest") if isinstance(payload.get("model_digest"), str) else None),
            build_input_sha256=(payload.get("build_input_sha256") if isinstance(payload.get("build_input_sha256"), str) else None),
            progress=(
                dict(payload.get("progress"))
                if isinstance(payload.get("progress"), Mapping)
                else {}
            ),
            result=(dict(result) if result is not None and operation.state == "succeeded" else None),
            failure=(dict(failure) if failure is not None else None),
            supported_actions=actions,
            created_at=_iso(operation.created_at), updated_at=_iso(operation.updated_at),
        )


__all__ = [
    "OPERATION_KIND",
    "RecipeAuthorityResolver",
    "RecipeImageAvailabilityClaim",
    "RecipeImageAvailabilityError",
    "RecipeImageAvailabilityService",
    "RecipeImageAvailabilityView",
    "RecipeImageBuilder",
]
