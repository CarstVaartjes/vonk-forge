"""Recipe update batches: requests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeUpdateCode,
    RuntimeImageCode,
    SecurityRefusalReason,
    WaitReason,
)

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from ..auth import MUTATION_ROLES
from ..catalog_queries import active_head_revision
from ..models import CatalogDocumentRevision, Job, User
from ..recipe_image_availability import (
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityRefused,
    RecipeImageAvailabilityUnknown,
)
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateChild,
    RecipeUpdateDocument,
    RecipeUpdateFailure,
    RecipeUpdateResponse,
    RecipeUpdateScope,
    read_update_document,
)
from ..revision_images import revision_images
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
)
from ..strict_json import serialize_json_value
from ..user_authority import serialize_user_authority

if TYPE_CHECKING:
    from .service import RecipeUpdateBatches

from .helpers import _binding_digest, _encoded, _now


class RequestsMixin:
    def _document(self, job: Job) -> RecipeUpdateDocument:
        try:
            document = read_update_document(job.payload)
        except (ValueError, TypeError) as error:
            raise RecipeImageAvailabilityUnknown(
                RecipeUpdateCode.OPERATION_INVALID,
                "stored update document is invalid",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                retryable=False,
            ) from error
        if job.kind != UPDATE_KIND or _binding_digest(document) != job.payload_digest:
            raise RecipeImageAvailabilityUnknown(
                RecipeUpdateCode.OPERATION_INVALID,
                "stored update scope does not match its accepted identity",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                retryable=False,
            )
        if any(
            child.operation_id == job.id or child.request_key == job.request_id
            for child in document.children
        ):
            raise RecipeImageAvailabilityUnknown(
                RecipeUpdateCode.OPERATION_INVALID,
                "update dependency graph contains a cycle",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                retryable=False,
            )
        return document

    @staticmethod
    def _authorize(session: Session, actor: str) -> None:
        serialize_user_authority(session)
        user = session.scalar(select(User).where(User.subject == actor))
        if (
            user is None
            or user.disabled_at is not None
            or user.role not in MUTATION_ROLES[("POST", "/api/recipe/update")]
        ):
            raise RecipeImageAvailabilityRefused(
                SecurityRefusalReason.RECIPE_UPDATE_AUTHORITY_DENIED.value,
                "recipe update authority is no longer available",
                retryable=False,
            )

    def _matching(
        self, job: Job, actor: str, scope: RecipeUpdateScope
    ) -> RecipeUpdateResponse:
        service = cast("RecipeUpdateBatches", self)
        if job.kind != UPDATE_KIND or job.actor != actor:
            raise RecipeImageAvailabilityInvalid(
                RecipeUpdateCode.REQUEST_KEY_REUSED,
                "request key was already used for another operation",
                reason=InvalidRequestReason.CONFLICT,
                retryable=False,
            )
        document = service._document(job)
        if document.request != scope:
            raise RecipeImageAvailabilityInvalid(
                RecipeUpdateCode.REQUEST_KEY_REUSED,
                "request key was already used for another scope",
                reason=InvalidRequestReason.CONFLICT,
                retryable=False,
            )
        return service._view(job, document)

    def _replay(
        self, request_id: str, actor: str, scope: RecipeUpdateScope
    ) -> RecipeUpdateResponse | None:
        service = cast("RecipeUpdateBatches", self)
        with service.sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            return (
                None if existing is None else service._matching(existing, actor, scope)
            )

    def _cached_revisions(self) -> list[str]:
        # SQL names each head's images (the builds of its recipe); only managed
        # bytes prove cache presence. Historical successful Jobs do not
        # participate in selection.
        service = cast("RecipeUpdateBatches", self)
        with service.sessions() as session:
            heads = {
                (row.publisher, row.slug): row.id
                for row in session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        active_head_revision(),
                    )
                )
            }
            images = revision_images(session, set(heads.values()), same_source=True)
        cached: dict[tuple[str, str], str] = {}
        for logical, head_id in heads.items():
            for image in images.get(head_id, ()):
                try:
                    receipt = service.owner._storage.read_receipt(image.archive_sha256)
                    service.owner._storage.existing_archive(
                        receipt.oci_archive_sha256, receipt.image_bytes
                    )
                except RuntimeImagePreparationError as error:
                    if error.code == RuntimeImageCode.CACHE_MISSING or isinstance(
                        error.__cause__, FileNotFoundError
                    ):
                        continue
                    raise
                cached[logical] = head_id
                break
        return [cached[logical] for logical in sorted(cached)]

    def start(
        self, *, actor: str, request_id: str, selectors: list[str] | None, all: bool
    ) -> RecipeUpdateResponse:
        service = cast("RecipeUpdateBatches", self)
        scope = RecipeUpdateScope(
            all=all, selectors=[] if selectors is None else selectors
        )
        replay = service._replay(request_id, actor, scope)
        if replay is not None:
            return replay
        with service.sessions.begin() as session:
            service._authorize(session, actor)
        revision_ids = (
            service._cached_revisions()
            if all
            else [
                service.owner._resolve_recipe_selector(selector)
                for selector in scope.selectors
            ]
        )
        revision_ids = list(dict.fromkeys(revision_ids))
        children = []
        with service.sessions() as session:
            for revision_id in revision_ids:
                revision = session.get(CatalogDocumentRevision, revision_id)
                if (
                    revision is None
                    or revision.state != "active"
                    or revision.execution_key is None
                ):
                    raise RecipeImageAvailabilityInvalid(
                        RecipeUpdateCode.SCOPE_INVALID,
                        "selected recipe revision is no longer available",
                        retryable=False,
                    )
                children.append(
                    RecipeUpdateChild(
                        recipe_revision_id=revision.id,
                        recipe_content_sha256=revision.content_digest,
                        effective_execution_key=revision.execution_key,
                        recipe_name=f"{revision.publisher}/{revision.slug}",
                        request_key=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"vonk:recipe-update:{request_id}:{revision.id}",
                            )
                        ),
                    )
                )
        document = RecipeUpdateDocument(request=scope, children=children)
        now = _now(service.owner._clock())
        document.next_attempt_at = now if children else None
        job = service._lifecycle.new_batch(
            allowed=bool(children),
            id=str(uuid.uuid4()),
            request_id=request_id,
            actor=actor,
            kind=UPDATE_KIND,
            authority_revision=_binding_digest(document),
            targets=revision_ids,
            payload_digest=_binding_digest(document),
            payload=serialize_json_value(document),
            result=None,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        service._reserve_document_budget(job, document)
        try:
            with service.sessions.begin() as session:
                service._authorize(session, actor)
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is not None:
                    return service._matching(existing, actor, scope)
                session.add(job)
                session.flush()
                return service._view(job, document)
        except IntegrityError:
            replay = service._replay(request_id, actor, scope)
            if replay is None:
                raise
            return replay

    def _reserve_document_budget(
        self, job: Job, document: RecipeUpdateDocument
    ) -> None:
        # Reserve the largest bounded child-observation fields at admission so
        # later failures cannot make the complete parent unreadable by the CLI.
        service = cast("RecipeUpdateBatches", self)
        now = datetime.max.replace(tzinfo=UTC)
        worst = document.model_copy(deep=True)
        for index, child in enumerate(worst.children):
            suffix = str(index)
            child.operation_id = "\x01" * (128 - len(suffix)) + suffix
            child.state = LifecycleState.CANCELLED
            child.failure = RecipeUpdateFailure(
                code="x" * 96, detail="\x01" * 512, retryable=False
            )
            child.observed_at = child.retry_at = now
        worst.claim_owner = "\x01" * 128
        worst.claim_until = worst.next_attempt_at = now
        worst.cancellation = RecipeOperationCancellationResult(
            cancel_requested=True,
            cancel_requested_at=now,
            cancel_request_id="00000000-0000-4000-8000-000000000001",
            cancel_actor="\x01" * 256,
            reason="\x01" * 512,
        )
        response = service._view(job, worst).model_copy(
            update={"attempt": 2_147_483_647, "state": LifecycleState.CANCELLED}
        )
        size = max(len(_encoded(worst)), len(_encoded(response)))
        if size > MAX_CONTROL_DOCUMENT_BYTES:
            raise RecipeImageAvailabilityInvalid(
                RecipeUpdateCode.SCOPE_INVALID,
                f"complete update scope requires up to {size} bytes; document limit is {MAX_CONTROL_DOCUMENT_BYTES} bytes",
                retryable=False,
            )
