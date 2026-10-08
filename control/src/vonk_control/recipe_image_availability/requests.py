"""Requests for exact recipe image availability."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    RecipeImageCode,
    RecipeUpdateCode,
    SecurityRefusalError,
    WaitReason,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    has_pending_removal,
    lock_reference_gates,
)
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilityRetry,
    AvailabilityRuntime,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import (
    CatalogDocumentRevision,
    Job,
)
from ..operation_blockers import (
    OperationBlocker,
    make_blocker,
)
from ..operation_contract import AvailabilityOperationFailure
from ..recipe_availability_intent import (
    RecipeAvailabilityIntent,
    RecipeRevisionIntent,
    RecipeSelectorIntent,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    lock_availability_build_dependency,
)
from ..recipe_image_availability_view_contract import (
    RecipeImageAvailabilityView,
)
from ..revision_images import revision_archives
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .contracts import (
    _PREPARATION_CHAIN_LIMIT,
    _PREPARATION_RECHECK_QUIET,
    _PREPARATION_RETRY_QUIET,
    _SUCCEEDED,
    OPERATION_KIND,
    SCHEMA_VERSION,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityUnknown,
    _canonical_recipe,
    _digest,
    _iso,
    _known_total,
    _optional_digest,
    _progress,
    _read,
    _retry_after,
    _retryable,
)

if TYPE_CHECKING:
    from ..recipe_update_batches import RecipeUpdateClaim
    from .service import RecipeImageAvailabilityService


def start(
    self: RecipeImageAvailabilityService,
    recipe_revision_id: str,
    *,
    actor: str,
    request_id: str,
    model_digest: str | None = None,
    build_input_sha256: str | None = None,
    effective_execution_key: str | None = None,
    force: bool = False,
    force_rebuild: bool = False,
) -> RecipeImageAvailabilityView:
    """Refresh metadata and queue one exact selected Recipe operation."""

    if not isinstance(recipe_revision_id, str) or not recipe_revision_id.strip():
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.RECIPE_INVALID, "recipe revision is required"
        )
    if force and force_rebuild:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.ACTION_INVALID,
            "force cannot be combined with an explicit image action",
        )
    model_digest = _optional_digest(model_digest, field="model_digest")
    build_input_sha256 = _optional_digest(
        build_input_sha256, field="build_input_sha256"
    )
    effective_execution_key = _optional_digest(
        effective_execution_key, field="effective_execution_key"
    )
    intent = RecipeRevisionIntent(
        recipe_revision_id=recipe_revision_id,
        force=force,
        force_rebuild=force_rebuild,
        model_digest=model_digest,
        build_input_sha256=build_input_sha256,
        effective_execution_key=effective_execution_key,
    )
    return self._start_request(intent, actor=actor, request_id=request_id)


def cancel_profile_preparation(
    self: RecipeImageAvailabilityService,
    recipe_revision_id: str,
    *,
    actor: str,
    reason: str,
    application_id: str | None = None,
) -> tuple[str, ...]:
    """Cancel the pending preparation a profile load asked for, if any.

    Only the chain of deterministic requests ``ensure_preparation`` makes
    is touched. The build layer still refuses to cancel an image another
    accepted consumer needs.
    """

    namespace = uuid.NAMESPACE_URL
    request_id = str(
        uuid.uuid5(
            namespace,
            f"vonk-forge:profile-preparation:{recipe_revision_id}"
            + (f":{application_id}" if application_id else ""),
        )
    )
    cancelled: list[str] = []
    for _ in range(_PREPARATION_CHAIN_LIMIT):
        with self._sessions.begin() as session:
            job = session.scalar(
                select(Job)
                .where(Job.kind == OPERATION_KIND, Job.request_id == request_id)
                .with_for_update(nowait=True)
            )
            if job is None:
                break
            if job.state in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
            ):
                self._request_cancellation(
                    session,
                    job,
                    actor=actor,
                    request_id=str(
                        uuid.uuid5(
                            namespace,
                            f"vonk-forge:profile-preparation-cancel:{job.id}",
                        )
                    ),
                    reason=reason,
                    # Cleanup belongs to the accepted profile cancellation,
                    # not the current permissions of its original author.
                    authorize=False,
                )
                cancelled.append(job.id)
                break
            if job.state not in {"failed", "cancelled", *_SUCCEEDED}:
                break
            request_id = str(
                uuid.uuid5(
                    namespace,
                    f"vonk-forge:profile-preparation:{recipe_revision_id}:{job.id}",
                )
            )
    return tuple(cancelled)


def ensure_preparation(
    self: RecipeImageAvailabilityService,
    recipe_revision_id: str,
    *,
    actor: str,
    application_id: str | None = None,
) -> tuple[OperationBlocker, ...]:
    """Start, or find, the preparation a load asks for; say what it waits on.

    One deterministic request identity per revision keeps repeated asks on
    the same durable operation. A preparation that ended for good is asked
    again only after a quiet period, so a hopeless one cannot spin.
    """

    namespace = uuid.NAMESPACE_URL
    request_id = str(
        uuid.uuid5(
            namespace,
            f"vonk-forge:profile-preparation:{recipe_revision_id}"
            + (f":{application_id}" if application_id else ""),
        )
    )
    for _ in range(_PREPARATION_CHAIN_LIMIT):
        view = self.start(recipe_revision_id, actor=actor, request_id=request_id)
        if view.state in _SUCCEEDED:
            # The caller asks only because its plan still lacks the image
            # or model, so an earlier success is stale (the image was
            # removed or the revision's build changed). Replaying it would
            # park the load for ever; ask again under a new identity,
            # paced so an evidence mismatch cannot spin.
            updated = datetime.fromisoformat(view.updated_at)
            updated = updated if updated.tzinfo else updated.replace(tzinfo=UTC)
            now = self._clock()
            now = now if now.tzinfo else now.replace(tzinfo=UTC)
            if now < updated + _PREPARATION_RECHECK_QUIET:
                return (
                    make_blocker(
                        RecipeImageCode.PREPARING,
                        "Preparing finished a moment ago; checking that "
                        "the model and runtime image are in place.",
                        severity="info",
                    ),
                )
            request_id = str(
                uuid.uuid5(
                    namespace,
                    f"vonk-forge:profile-preparation:{recipe_revision_id}:{view.id}",
                )
            )
            continue
        if view.state not in {"failed", "cancelled"}:
            break
        updated = datetime.fromisoformat(view.updated_at)
        updated = updated if updated.tzinfo else updated.replace(tzinfo=UTC)
        retry_at = updated + _PREPARATION_RETRY_QUIET
        now = self._clock()
        now = now if now.tzinfo else now.replace(tzinfo=UTC)
        if now < retry_at:
            failure = view.failure_evidence
            return (
                make_blocker(
                    failure.code if failure else RecipeImageCode.PREPARATION_FAILED,
                    f"Preparing this recipe failed: "
                    f"{failure.detail if failure else view.state}. "
                    f"The Controller asks again after {retry_at.isoformat()}.",
                    severity="error",
                ),
            )
        request_id = str(
            uuid.uuid5(
                namespace,
                f"vonk-forge:profile-preparation:{recipe_revision_id}:{view.id}",
            )
        )
    else:
        return (
            make_blocker(
                RecipeImageCode.PREPARATION_EXHAUSTED,
                "The bounded preparation attempts ended without the required assets.",
                severity="error",
            ),
        )
    if view.state == "succeeded":
        return ()
    return view.blockers or (
        make_blocker(
            RecipeImageCode.PREPARING,
            f"Preparing the model and runtime image ({view.measurement.phase})."
            if view.measurement is not None
            else "Preparing progress is currently unavailable; the Controller retries observation.",
            severity="info",
        ),
    )


def _refresh_authority(
    self: RecipeImageAvailabilityService, recipe_revision_id: str, *, force: bool
) -> tuple[RecipeDefinition, AvailabilityRuntime | None]:
    assert self._authority is not None
    try:
        recipe, runtime = self._authority(recipe_revision_id, force=force)
        parsed = _read(AvailabilityRuntime, runtime, subject=recipe_revision_id)
        return _canonical_recipe(recipe), parsed
    except (RecipeImageAvailabilityError, SecurityRefusalError, InvalidRequestError):
        raise
    except Exception as error:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.METADATA_REFRESH_FAILED,
            "latest recipe metadata could not be refreshed",
            retryable=_retryable(error),
            retry_after_seconds=_retry_after(error),
            recovery_actions=("retry",) if _retryable(error) else ("inspect",),
        ) from error


def _start_request(
    self: RecipeImageAvailabilityService,
    intent: RecipeSelectorIntent | RecipeRevisionIntent,
    *,
    actor: str,
    request_id: str,
    update_claim: RecipeUpdateClaim | None = None,
) -> RecipeImageAvailabilityView:
    """Bound metadata/admission observation before any accepted image effect.

    Replays are reconciled by the ordinary admission path on every attempt.
    Exhaustion ends this observation with evidence, holding no claim or gate;
    a later request may try again without retiring poisoned bookkeeping.
    """
    deadline = time.monotonic() + 0.25
    delay = 0.01
    attempts = 0
    while True:
        attempts += 1
        try:
            return _start_request_once(
                self,
                intent,
                actor=actor,
                request_id=request_id,
                update_claim=update_claim,
            )
        except RecipeImageAvailabilityUnknown as error:
            remaining = deadline - time.monotonic()
            if not error.retryable or remaining <= 0:
                return _unaccepted_request_end(
                    self,
                    intent,
                    actor=actor,
                    request_id=request_id,
                    error=error,
                    attempts=attempts,
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)


def _unaccepted_request_end(
    self: RecipeImageAvailabilityService,
    intent: RecipeAvailabilityIntent,
    *,
    actor: str,
    request_id: str,
    error: RecipeImageAvailabilityUnknown,
    attempts: int,
) -> RecipeImageAvailabilityView:
    """No effect was accepted: report a typed end without inventing a plan."""
    now = self._clock()
    return RecipeImageAvailabilityView(
        id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:availability-observation:{actor}:{request_id}",
            )
        ),
        request_id=request_id,
        request=intent,
        kind=OPERATION_KIND,
        state=LifecycleState.FAILED.value,
        attempt=attempts,
        recipe_revision_id=(
            intent.recipe_revision_id
            if isinstance(intent, RecipeRevisionIntent)
            else None
        ),
        recipe_content_sha256=None,
        model_digest=None,
        build_input_sha256=None,
        progress=None,
        image_progress=None,
        result=None,
        failure=AvailabilityOperationFailure(
            code=error.code,
            detail=error.detail,
            retryable=False,
            recovery_actions=[],
        ),
        supported_actions=(),
        created_at=_iso(now),
        updated_at=_iso(now),
        residue=retire_as_unknown(
            "recipe-image.admission",
            request_id,
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            error.detail,
        ),
    )


def _start_request_once(
    self: RecipeImageAvailabilityService,
    intent: RecipeSelectorIntent | RecipeRevisionIntent,
    *,
    actor: str,
    request_id: str,
    update_claim: RecipeUpdateClaim | None = None,
) -> RecipeImageAvailabilityView:
    existing = self._request_replay(request_id, actor=actor, intent=intent)
    if existing is not None:
        return existing
    if isinstance(intent, RecipeSelectorIntent):
        recipe_revision_id = self._resolve_recipe_selector(intent.selector)
        model_digest = build_input_sha256 = effective_execution_key = None
        force_rebuild = False
    else:
        recipe_revision_id = intent.recipe_revision_id
        model_digest = intent.model_digest
        build_input_sha256 = intent.build_input_sha256
        effective_execution_key = intent.effective_execution_key
        force_rebuild = intent.force_rebuild
    force = intent.force
    if self._authority is None:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.METADATA_REFRESH_UNAVAILABLE,
            "latest recipe metadata could not be refreshed",
            reason=InvalidRequestReason.NOT_FOUND,
        )
    raw_recipe, runtime = self._refresh_authority(recipe_revision_id, force=force)
    if isinstance(intent, RecipeSelectorIntent):
        # The refresh may have published a newer head; the selector means
        # the current revision, so follow it instead of reporting staleness.
        current_revision_id = self._resolve_recipe_selector(intent.selector)
        if current_revision_id != recipe_revision_id:
            recipe_revision_id = current_revision_id
            raw_recipe, runtime = self._refresh_authority(
                recipe_revision_id, force=force
            )
    _canonical_recipe(raw_recipe)
    if runtime is None:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.RUNTIME_INVALID,
            "selected recipe runtime projection is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    try:
        with self._sessions.begin() as session:
            if update_claim is not None:
                if not isinstance(intent, RecipeRevisionIntent):
                    raise RecipeImageAvailabilityInvalid(
                        RecipeUpdateCode.OPERATION_INVALID,
                        "update child requires an exact revision",
                    )
                self._updates.authorize_child(
                    session, update_claim, actor, request_id, intent
                )
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if (
                revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
            ):
                raise RecipeImageAvailabilityInvalid(
                    RecipeImageCode.RECIPE_UNAVAILABLE,
                    "selected recipe revision is unavailable or inactive",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            if effective_execution_key is None:
                effective_execution_key = revision.execution_key
            if effective_execution_key != revision.execution_key:
                raise RecipeImageAvailabilityInvalid(
                    RecipeImageCode.IDENTITY_CONFLICT,
                    "selected recipe execution identity changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if force:
                force_rebuild = True
            runtime_build_input = runtime.build_input_sha256
            provisional_intent = runtime.input_intent_sha256
            if not isinstance(runtime_build_input, str) and not isinstance(
                provisional_intent, str
            ):
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.BUILD_INPUT_MISSING,
                    "authoritative runtime projection lacks the exact build input digest",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if isinstance(runtime_build_input, str):
                runtime_build_input = _digest(
                    runtime_build_input, field="build_input_sha256"
                )
                if build_input_sha256 is None:
                    build_input_sha256 = runtime_build_input
                elif build_input_sha256 != runtime_build_input:
                    raise RecipeImageAvailabilityInvalid(
                        RecipeImageCode.IDENTITY_CONFLICT,
                        "submitted build input does not match authoritative runtime metadata",
                        reason=InvalidRequestReason.CONFLICT,
                    )
            identity_key = build_input_sha256
            recipe = _canonical_recipe(read_row_column(revision, "document"))
            payload = AvailabilityJobPayload(
                schema_version=SCHEMA_VERSION,
                kind=OPERATION_KIND,
                request=intent,
                recipe_revision_id=recipe_revision_id,
                recipe_content_sha256=revision.content_digest,
                effective_execution_key=effective_execution_key,
                model_digest=model_digest,
                build_input_sha256=build_input_sha256,
                identity_key=identity_key,
                recipe=recipe,
                runtime=runtime,
                force_rebuild=force_rebuild,
                progress=_progress("prepare", total_bytes=_known_total(runtime)),
                retry=AvailabilityRetry(automatic_attempts=0, operator_retries=0),
            )
            encoded = canonical_message(payload)
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._matching_request(existing, actor=actor, intent=intent)
            current_archives = tuple(revision_archives(session, [recipe_revision_id]))
            # This accepted request owns a durable wait, not available bytes.
            # Gates still fence storage effects until the worker reconciles them.
            lock_reference_gates(
                session,
                (
                    ArtifactIdentity("runtime-image", archive)
                    for archive in current_archives
                ),
                now=self._clock(),
            )
            payload = payload.model_copy(
                update={"removal_archives": list(current_archives)}
            )
            if has_pending_removal(
                session,
                (
                    ArtifactIdentity("runtime-image", archive)
                    for archive in current_archives
                ),
            ):
                payload = payload.model_copy(
                    update={
                        "blockers": [
                            make_blocker(
                                ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                                "Waiting for the prior image removal fence to settle",
                                severity="info",
                            )
                        ]
                    }
                )
            encoded = canonical_message(payload)
            self._lock_build_consumer(session, payload)
            now = self._clock()
            operation = self._lifecycle.new_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=OPERATION_KIND,
                actor=actor,
                authority_revision=recipe_revision_id,
                targets=[recipe_revision_id],
                payload_digest=hashlib.sha256(encoded).hexdigest(),
                payload=serialize_json_value(payload),
                result=None,
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            self._cancel_older_preparations(session, newer_revision=revision, now=now)
            return self._view(operation)
    except IntegrityError:
        replay = self._request_replay(request_id, actor=actor, intent=intent)
        if replay is None:
            raise
        return replay


def _lock_build_consumer(session: Session, payload: AvailabilityJobPayload) -> None:
    try:
        lock_availability_build_dependency(session, payload.model_dump(mode="json"))
    except BuildConsumerError as error:
        raise RecipeImageAvailabilityUnknown(
            error.code,
            str(error),
            retryable=error.retryable,
            recovery_actions=("retry",) if error.retryable else (),
        ) from error
