"""Persistence for exact recipe image availability."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
    OperationProgress,
    ProgressPhase,
    RecipeImageCode,
    WaitReason,
    canonical_message,
)

from .. import job_states
from ..catalog_queries import active_head_revision
from ..job_documents import (
    AvailabilityJobPayload,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..model_cache import (
    ModelCacheRemovalScope,
)
from ..models import (
    CatalogDocumentRevision,
    Job,
)
from ..recipe_availability_intent import (
    RecipeSelectorIntent,
)
from ..recipe_image_availability_reader_contract import (
    RecoveredAvailabilityPayload,
    StoredAvailabilityIdentity,
)
from ..recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from ..recipe_image_removal_contract import (
    RecipeCacheRemovalIntent,
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalPlan,
    RecipeCacheRemovalResult,
)
from ..revision_images import revision_images
from ..stored_json import Residue, read_row_column
from .contracts import (
    _ACTIVE,
    _SHA256,
    _WAITING,
    REMOVE_OPERATION_KIND,
    SCHEMA_VERSION,
    ModelCacheRemovalCoordinator,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityUnknown,
    _is_digest,
    _iso,
    _read,
    _RecipeRemovalSelection,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _payload(
    self: RecipeImageAvailabilityService, operation: Job
) -> AvailabilityJobPayload | Residue:
    value = read_row_column(operation, "payload")
    if isinstance(value, AvailabilityJobPayload):
        return value
    identity = _read(
        StoredAvailabilityIdentity, operation.payload, subject=operation.id
    )
    if identity is not None:
        try:
            recipe = self._stored_recipe(identity)
            if isinstance(recipe, Residue):
                return recipe
            # Repair is confined to the JSON decoding boundary, then the
            # complete current contract must validate again.
            document = json.loads(canonical_message(operation.payload))
            if isinstance(document, dict):
                document["recipe"] = recipe.model_dump(mode="json")
                recovered = _read(
                    RecoveredAvailabilityPayload,
                    document,
                    subject=operation.id,
                )
                if recovered is not None:
                    return AvailabilityJobPayload.model_validate_json(
                        canonical_message(recovered)
                    )
        except (RecipeImageAvailabilityError, TypeError, ValueError):
            pass
    if isinstance(value, Residue):
        return value
    return retire_as_unknown(
        "jobs.payload", operation.id, BookkeepingReason.ROW_INCOMPLETE
    )


def _recipe_removal_selection_in_session(
    self: RecipeImageAvailabilityService,
    session: Session,
    selector: str,
    *,
    with_model: bool,
) -> _RecipeRemovalSelection:
    revision_id = self._resolve_recipe_selector_in_session(session, selector)
    revision = session.get(
        CatalogDocumentRevision,
        revision_id,
        populate_existing=True,
    )
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.state != "active"
        or not isinstance(revision.content_digest, str)
    ):
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_MISSING,
            "selected recipe revision is not active",
            reason=InvalidRequestReason.NOT_FOUND,
        )
    # The recipe document itself is not read: its cache is selected by the
    # revision identity, so a revision whose stored document is damaged can
    # still have its cache reviewed and removed.

    # The images the recipe's builds produced are the ones its removal
    # selects; the reference scan keeps whatever another owner still uses.
    image_sizes: dict[str, int] = {}
    for image in revision_images(session, [revision_id]).get(revision_id, ()):
        if not _is_digest(image.archive_sha256):
            # A stored archive digest that is not a digest names nothing that
            # can be removed: the damage is recorded and the image skipped.
            retire_as_unknown(
                "recipe-image.removal-archive",
                revision_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "stored runtime image archive digest is not a SHA-256 digest",
            )
            continue
        image_sizes.setdefault(image.archive_sha256, image.image_bytes)

    model_scope: ModelCacheRemovalScope | None = None
    if with_model:
        if self._model_cache is None:
            raise RecipeImageAvailabilityInvalid(
                ModelCacheCode.UNAVAILABLE,
                "model cache removal is unavailable",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        model_scope = cast(
            ModelCacheRemovalCoordinator, self._model_cache
        ).recipe_removal_scope_in_session(session, recipe_revision_id=revision_id)
    return _RecipeRemovalSelection(
        revision_id=revision_id,
        revision_content_sha256=revision.content_digest,
        image_archives=tuple(sorted(image_sizes)),
        image_expected_bytes=tuple(sorted(image_sizes.items())),
        model_scope=model_scope,
    )


def _resolve_recipe_selector(
    self: RecipeImageAvailabilityService, selector: str
) -> str:
    """Resolve logical selectors to the current head, retaining exact pins."""

    if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
        )
    selector = selector.strip().casefold()
    with self._sessions() as session:
        return self._resolve_recipe_selector_in_session(session, selector)


def _resolve_recipe_selector_in_session(session: Session, selector: str) -> str:
    """Resolve one already-normalized recipe selector in its SQL snapshot."""

    if _SHA256.fullmatch(selector):
        rows = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.content_digest == selector,
                )
            )
        )
    elif re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        selector,
    ):
        rows = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    (CatalogDocumentRevision.id == selector)
                    | (
                        (CatalogDocumentRevision.document_id == selector)
                        & active_head_revision()
                    ),
                )
            )
        )
    else:
        if "/" in selector:
            publisher, slug = selector.split("/", 1)
            query = select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
                CatalogDocumentRevision.publisher == publisher,
                CatalogDocumentRevision.slug == slug,
            )
        else:
            query = select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
                CatalogDocumentRevision.slug == selector,
            )
        rows = list(session.scalars(query.where(active_head_revision())))
    if not rows:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_MISSING,
            "recipe selector was not found",
            reason=InvalidRequestReason.NOT_FOUND,
        )
    if len(rows) != 1:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_AMBIGUOUS,
            "recipe selector matches multiple recipes",
            reason=InvalidRequestReason.CONFLICT,
        )
    return rows[0].id


def start_selector(
    self: RecipeImageAvailabilityService,
    selector: str,
    *,
    actor: str,
    request_id: str,
    force: bool = False,
) -> RecipeImageAvailabilityView:
    """Bind a selected recipe once under the caller's request identity."""

    intent = RecipeSelectorIntent(selector=selector, force=force)
    return self._start_request(intent, actor=actor, request_id=request_id)


def _removal_payload_digest(payload: RecipeCacheRemovalPlan) -> str:
    return hashlib.sha256(canonical_message(payload)).hexdigest()


def _read_removal_owner(
    self: RecipeImageAvailabilityService, operation: Job
) -> RecipeCacheRemovalOwner:
    try:
        owner = read_row_column(operation, "payload")
    except (TypeError, ValueError):
        owner = None
    if not isinstance(owner, RecipeCacheRemovalOwner):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "stored recipe removal owner is malformed",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    plan = owner.plan
    intent = plan.intent
    payload = plan
    if (
        operation.kind != REMOVE_OPERATION_KIND
        or operation.state
        not in job_states.words(
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.BACKOFF,
            LifecycleState.SUCCEEDED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        )
        or operation.actor != intent.actor
        or operation.request_id != intent.request_key
        or operation.authority_revision != intent.recipe_revision_id
        or operation.payload_digest != self._removal_payload_digest(payload)
        or operation.targets != []
    ):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "stored recipe removal plan does not match its Job owner",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if operation.state in job_states.words(
        LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
    ) and (
        owner.checkpoint.failure is not None and not owner.checkpoint.failure.retryable
    ):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "active recipe removal has a terminal failure checkpoint",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if operation.state == "failed" and (
        owner.checkpoint.failure is None or owner.checkpoint.failure.retryable
    ):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "failed recipe removal has no terminal failure checkpoint",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return owner


def _read_removal_intent(
    self: RecipeImageAvailabilityService, operation: Job
) -> RecipeCacheRemovalIntent:
    return self._read_removal_owner(operation).plan.intent


def _removal_progress_document(
    operation: Job,
    owner: RecipeCacheRemovalOwner,
) -> OperationProgress:
    checkpoint = owner.checkpoint
    total_items = len(owner.plan.image_archives) + len(owner.plan.model_children)
    completed_items = checkpoint.image_index + checkpoint.model_index
    reclaimed_bytes = (
        checkpoint.image_reclaimed_bytes + checkpoint.model_reclaimed_bytes
    )
    successful = operation.state == "succeeded"
    progress = OperationProgress(
        phase=(
            ProgressPhase.COMPLETED
            if successful
            else ProgressPhase.FAILED
            if operation.state in {"failed", "cancelled"}
            else ProgressPhase.RECLAIMING
        ),
        completed_bytes=reclaimed_bytes,
        total_bytes=reclaimed_bytes if successful else None,
        total_bytes_known=successful,
        completed_items=completed_items,
        total_items=total_items,
        observed_at=_iso(operation.updated_at),
        activity=_ACTIVE if operation.state == "running" else _WAITING,
    )
    return progress


def _read_removal_result(
    self: RecipeImageAvailabilityService,
    operation: Job,
    intent: RecipeCacheRemovalIntent,
) -> RecipeCacheRemovalStatus:
    """Rebuild the read projection from the authoritative plan and checkpoint."""
    owner = self._read_removal_owner(operation)
    return removal_status(self, operation, owner)


def removal_status(
    self: RecipeImageAvailabilityService, operation: Job, owner: RecipeCacheRemovalOwner
) -> RecipeCacheRemovalStatus:
    """Project a known typed owner without independently admitting it again."""
    intent = owner.plan.intent
    if operation.state == "succeeded":
        self._stored_removal_result(operation, intent, owner)
    status = RecipeCacheRemovalStatus(
        schema_version=SCHEMA_VERSION,
        action=intent.action,
        selector=intent.selector,
        request_key=intent.request_key,
        operation_id=operation.id,
        recipe_revision_id=intent.recipe_revision_id,
        review_digest=intent.review_digest,
        with_model=intent.with_model,
        state=operation.state,
        progress=self._removal_progress_document(operation, owner),
        reclaimed_bytes=owner.checkpoint.image_reclaimed_bytes
        + owner.checkpoint.model_reclaimed_bytes,
        preserved=["profile-assignments", "spark-local-copies"]
        + ([] if intent.with_model else ["model-download"]),
        failure=owner.checkpoint.failure,
        next_actions=[]
        if owner.checkpoint.failure is None
        else [action.value for action in owner.checkpoint.failure.recovery_actions],
        cancelled_operations=[],
        cancelled_builds=[],
        model_removals=[child.operation_id for child in owner.plan.model_children],
    )
    return status


def _stored_removal_result(
    operation: Job,
    intent: RecipeCacheRemovalIntent,
    owner: RecipeCacheRemovalOwner,
) -> RecipeCacheRemovalResult | None:
    """The stored result of a finished removal, or ``None`` when it is
    missing, malformed or disagrees with the accepted intent."""

    result = read_row_column(operation, "result")
    if not isinstance(result, RecipeCacheRemovalResult):
        return None
    if (
        result.action != intent.action
        or result.selector != intent.selector
        or result.request_key != intent.request_key
        or result.operation_id != operation.id
        or result.recipe_revision_id != intent.recipe_revision_id
        or result.review_digest != intent.review_digest
        or result.with_model is not intent.with_model
        or result.reclaimed_bytes
        != owner.checkpoint.image_reclaimed_bytes
        + owner.checkpoint.model_reclaimed_bytes
        or result.model_removals
        != [item.operation_id for item in owner.plan.model_children]
        or owner.checkpoint.image_index != len(owner.plan.image_archives)
        or owner.checkpoint.model_index != len(owner.plan.model_children)
        or owner.checkpoint.failure is not None
    ):
        return None
    return result


def _replay_removal(
    self: RecipeImageAvailabilityService,
    operation: Job,
    *,
    selector: str,
    actor: str,
    request_id: str,
    with_model: bool,
) -> RecipeCacheRemovalStatus:
    if operation.kind != REMOVE_OPERATION_KIND:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.REQUEST_KEY_REUSED,
            "request key was already used for another operation",
            reason=InvalidRequestReason.CONFLICT,
        )
    owner = self._read_removal_owner(operation)
    intent = owner.plan.intent
    if (
        intent.selector != selector
        or intent.actor != actor
        or intent.request_key != request_id
        or intent.with_model is not with_model
    ):
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.REQUEST_KEY_REUSED,
            "request key was already used for another removal intent",
            reason=InvalidRequestReason.CONFLICT,
        )
    return self._read_removal_result(operation, intent)
