"""Recipe availability's durable parent/child coordinator, using existing Jobs.

Claims are short SQL transactions. Child admission and cache inspection happen
outside them; each effect is recorded only while its parent fence still holds.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import OperationProgress

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .auth import MUTATION_ROLES
from .catalog_queries import active_head_revision
from .logging import redact_text
from .models import CatalogDocumentRevision, Job, RuntimeImageAuthorization, User
from .operation_api import (
    OperationListPage,
    OperationProvider,
    OperationQuery,
    _activity_keyset_filter,
)
from .recipe_availability_intent import RecipeRevisionIntent
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateBinding,
    RecipeUpdateChild,
    RecipeUpdateDocument,
    RecipeUpdateFailure,
    RecipeUpdateResponse,
    RecipeUpdateScope,
    UpdateState,
    read_update_document,
)
from .runtime_image_preparation import (
    RuntimeImagePreparationError,
    resolve_persisted_runtime_image_receipt,
)
from .strict_json import serialize_json_value
from .user_authority import serialize_user_authority

if TYPE_CHECKING:
    from .recipe_image_availability import RecipeImageAvailabilityService

_SETTLED = frozenset({"succeeded", "failed", "cancelled"})
_OBSERVATION_INTERVAL = timedelta(seconds=2)


@dataclass(frozen=True)
class RecipeUpdateClaim:
    operation_id: str
    owner: str


def _encoded(document: object) -> bytes:
    return json.dumps(
        serialize_json_value(document),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _binding_digest(document: RecipeUpdateDocument) -> str:
    binding = RecipeUpdateBinding(
        request=document.request,
        children=[child.identity() for child in document.children],
    )
    return hashlib.sha256(_encoded(binding)).hexdigest()


def _now(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(code: str, detail: str, *, retryable: bool = False):
    # The helper belongs to this service; importing lazily avoids a module cycle.
    from .recipe_image_availability import RecipeImageAvailabilityError

    return RecipeImageAvailabilityError(code, detail, retryable=retryable)


class RecipeUpdateBatches:
    def __init__(
        self, owner: RecipeImageAvailabilityService, sessions: sessionmaker[Session]
    ) -> None:
        self.owner = owner
        self.sessions = sessions

    def _document(self, job: Job) -> RecipeUpdateDocument:
        try:
            document = read_update_document(job.payload)
        except (ValueError, TypeError) as error:
            raise _error(
                "recipe_update.operation_invalid", "stored update document is invalid"
            ) from error
        if job.kind != UPDATE_KIND or _binding_digest(document) != job.payload_digest:
            raise _error(
                "recipe_update.operation_invalid",
                "stored update scope does not match its accepted identity",
            )
        if any(
            child.operation_id == job.id or child.request_key == job.request_id
            for child in document.children
        ):
            raise _error(
                "recipe_update.operation_invalid",
                "update dependency graph contains a cycle",
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
            raise _error(
                "recipe_update.authority_denied",
                "recipe update authority is no longer available",
            )

    def _matching(
        self, job: Job, actor: str, scope: RecipeUpdateScope
    ) -> RecipeUpdateResponse:
        if job.kind != UPDATE_KIND or job.actor != actor:
            raise _error(
                "recipe_update.request_key_reused",
                "request key was already used for another operation",
            )
        document = self._document(job)
        if document.request != scope:
            raise _error(
                "recipe_update.request_key_reused",
                "request key was already used for another scope",
            )
        return self._view(job, document)

    def _replay(
        self, request_id: str, actor: str, scope: RecipeUpdateScope
    ) -> RecipeUpdateResponse | None:
        with self.sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            return None if existing is None else self._matching(existing, actor, scope)

    def _cached_revisions(self) -> list[str]:
        # SQL owns identity and authorization; only managed bytes prove cache
        # presence. Historical successful Jobs do not participate in selection.
        with self.sessions() as session:
            authorized = list(
                session.scalars(
                    select(RuntimeImageAuthorization).where(
                        RuntimeImageAuthorization.state == "authorized"
                    )
                )
            )
            revisions = {
                row.id: row
                for row in session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe"
                    )
                )
            }
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
        cached: dict[tuple[str, str], str] = {}
        for authorization in authorized:
            revision = revisions.get(authorization.recipe_revision_id)
            if revision is None or revision.state != "active":
                continue
            try:
                receipt = self.owner._storage.read_receipt(
                    authorization.oci_archive_sha256
                )
                self.owner._storage.verify_existing(
                    receipt.oci_archive_sha256, receipt.image_bytes
                )
            except RuntimeImagePreparationError as error:
                if error.code == "runtime_image.cache_missing" or isinstance(
                    error.__cause__, FileNotFoundError
                ):
                    continue
                raise
            with self.sessions() as session:
                resolve_persisted_runtime_image_receipt(
                    session,
                    recipe_revision_id=revision.id,
                    current_content_digest=revision.content_digest,
                    effective_execution_key=authorization.effective_execution_key,
                    receipt=receipt,
                )
            logical = (revision.publisher, revision.slug)
            if logical not in heads:
                raise _error(
                    "recipe_update.scope_invalid",
                    "a cached recipe has no current accepted revision",
                )
            cached[logical] = heads[logical]
        return [cached[logical] for logical in sorted(cached)]

    def start(
        self, *, actor: str, request_id: str, selectors: list[str] | None, all: bool
    ) -> RecipeUpdateResponse:
        scope = RecipeUpdateScope(
            all=all, selectors=[] if selectors is None else selectors
        )
        replay = self._replay(request_id, actor, scope)
        if replay is not None:
            return replay
        with self.sessions.begin() as session:
            self._authorize(session, actor)
        revision_ids = (
            self._cached_revisions()
            if all
            else [
                self.owner._resolve_recipe_selector(selector)
                for selector in scope.selectors
            ]
        )
        revision_ids = list(dict.fromkeys(revision_ids))
        children = []
        with self.sessions() as session:
            for revision_id in revision_ids:
                revision = session.get(CatalogDocumentRevision, revision_id)
                if (
                    revision is None
                    or revision.state != "active"
                    or revision.execution_key is None
                ):
                    raise _error(
                        "recipe_update.scope_invalid",
                        "selected recipe revision is no longer available",
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
        now = _now(self.owner._clock())
        document.next_attempt_at = now if children else None
        job = Job(
            id=str(uuid.uuid4()),
            request_id=request_id,
            actor=actor,
            kind=UPDATE_KIND,
            state="queued" if children else "succeeded",
            authority_revision=_binding_digest(document),
            targets=revision_ids,
            payload_digest=_binding_digest(document),
            payload=serialize_json_value(document),
            result=None,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        self._reserve_document_budget(job, document)
        try:
            with self.sessions.begin() as session:
                self._authorize(session, actor)
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is not None:
                    return self._matching(existing, actor, scope)
                session.add(job)
                session.flush()
                return self._view(job, document)
        except IntegrityError:
            replay = self._replay(request_id, actor, scope)
            if replay is None:
                raise
            return replay

    def _reserve_document_budget(
        self, job: Job, document: RecipeUpdateDocument
    ) -> None:
        # Reserve the largest bounded child-observation fields at admission so
        # later failures cannot make the complete parent unreadable by the CLI.
        now = datetime.max.replace(tzinfo=UTC)
        worst = document.model_copy(deep=True)
        for index, child in enumerate(worst.children):
            suffix = str(index)
            child.operation_id = "\x01" * (128 - len(suffix)) + suffix
            child.state = "cancelled"
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
        response = self._view(job, worst).model_copy(
            update={"attempt": 2_147_483_647, "state": "cancelled"}
        )
        size = max(len(_encoded(worst)), len(_encoded(response)))
        if size > MAX_CONTROL_DOCUMENT_BYTES:
            raise _error(
                "recipe_update.scope_invalid",
                f"complete update scope requires up to {size} bytes; document limit is {MAX_CONTROL_DOCUMENT_BYTES} bytes",
            )

    def _view(self, job: Job, document: RecipeUpdateDocument) -> RecipeUpdateResponse:
        complete = sum(child.state in _SETTLED for child in document.children)
        waiting = job.state in {"queued", "running", "cancelling"} and bool(
            document.children
        )
        return RecipeUpdateResponse(
            id=job.id,
            request_id=job.request_id,
            request=document.request,
            state=cast(UpdateState, job.state),
            attempt=job.current_attempt,
            children=document.children,
            cancellation=document.cancellation,
            progress=OperationProgress(
                phase="update" if waiting else "complete",
                completed_bytes=0,
                total_bytes_known=False,
                completed_items=complete,
                total_items=len(document.children),
            ),
            waiting_on="recipe operations" if waiting else None,
            wait_owner="recipe-image-availability" if waiting else None,
            next_attempt_at=document.claim_until or document.next_attempt_at,
            resume_condition="a child changes state or its next observation is due"
            if waiting
            else None,
            created_at=_now(job.created_at),
            updated_at=_now(job.updated_at),
        )

    def get(self, operation_id: str) -> RecipeUpdateResponse:
        with self.sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind != UPDATE_KIND:
                raise KeyError(operation_id)
            return self._view(job, self._document(job))

    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeUpdateResponse:
        """Stop future batch admissions and durably detach its exact children."""

        with self.sessions.begin() as session:
            self._authorize(session, actor)
            job = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == UPDATE_KIND)
                .with_for_update(nowait=True)
            )
            if job is None:
                raise KeyError(operation_id)
            self.owner._request_cancellation(
                session,
                job,
                actor=actor,
                request_id=request_id,
                reason=reason,
                authorize=False,
            )
        return self.get(operation_id)

    def reconcile_cancellations(self, *, limit: int = 8) -> int:
        """Reconcile accepted child operations without claiming the batch slot."""

        from .recipe_image_availability import (
            RecipeImageAvailabilityError,
            RecipeImageAvailabilityView,
        )

        if not 1 <= limit <= 100:
            raise ValueError("update cancellation limit is invalid")
        with self.sessions() as session:
            operation_ids = tuple(
                session.scalars(
                    select(Job.id)
                    .where(Job.kind == UPDATE_KIND, Job.state == "cancelling")
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit)
                )
            )
        progressed = 0
        for operation_id in operation_ids:
            with self.sessions() as session:
                parent = session.get(Job, operation_id)
                if parent is None or parent.state != "cancelling":
                    continue
                try:
                    document = self._document(parent)
                except RecipeImageAvailabilityError:
                    continue
                cancellation = document.cancellation
                actor = parent.actor
            if cancellation is None:
                continue
            observations: dict[str, RecipeImageAvailabilityView | None] = {}
            for child in document.children:
                if child.state in _SETTLED:
                    continue
                observed: RecipeImageAvailabilityView | None
                try:
                    value = self.owner.get_operator_request(
                        child.request_key, actor=actor
                    )
                except KeyError:
                    # Child admission and the parent's cancellation serialize on
                    # the same parent row. With cancellation holding that row,
                    # absence is durable evidence that this child never issued.
                    observed = None
                except RecipeImageAvailabilityError:
                    continue
                else:
                    if not isinstance(value, RecipeImageAvailabilityView):
                        continue
                    if (
                        value.request != self._intent(child)
                        or value.recipe_content_sha256 != child.recipe_content_sha256
                        or (
                            child.operation_id is not None
                            and child.operation_id != value.id
                        )
                    ):
                        continue
                    child_cancel_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-update-cancel:{operation_id}:{cancellation.cancel_request_id}:{child.request_key}",
                        )
                    )
                    child_cancellation = RecipeOperationCancellationResult(
                        cancel_requested=True,
                        cancel_requested_at=cancellation.cancel_requested_at,
                        cancel_request_id=child_cancel_id,
                        cancel_actor=cancellation.cancel_actor,
                        reason=cancellation.reason,
                    )
                    try:
                        self.owner._cancel_update_child(value.id, child_cancellation)
                        value = self.owner.get_operator_operation(value.id)
                    except RecipeImageAvailabilityError:
                        continue
                    except DBAPIError as error:
                        if getattr(error.orig, "sqlstate", None) == "55P03":
                            continue
                        raise
                    if not isinstance(value, RecipeImageAvailabilityView):
                        continue
                    observed = value
                observations[child.request_key] = observed
            if any(
                child.state not in _SETTLED and child.request_key not in observations
                for child in document.children
            ):
                continue
            now = _now(self.owner._clock())
            try:
                with self.sessions.begin() as session:
                    parent = session.scalar(
                        select(Job)
                        .where(Job.id == operation_id, Job.kind == UPDATE_KIND)
                        .with_for_update(nowait=True)
                    )
                    if parent is None or parent.state != "cancelling":
                        continue
                    current = self._document(parent)
                    if (
                        current.cancellation is None
                        or current.cancellation.cancel_request_id
                        != cancellation.cancel_request_id
                    ):
                        continue
                    children = []
                    for child in current.children:
                        observed = observations.get(child.request_key)
                        if (
                            child.state in _SETTLED
                            or child.request_key not in observations
                        ):
                            children.append(child)
                        elif observed is None:
                            children.append(
                                child.model_copy(
                                    update={
                                        "state": "cancelled",
                                        "failure": None,
                                        "retry_at": None,
                                        "observed_at": now,
                                    }
                                )
                            )
                        else:
                            state = cast(UpdateState, observed.state)
                            children.append(
                                child.model_copy(
                                    update={
                                        "operation_id": observed.id,
                                        "state": state,
                                        "failure": (
                                            None
                                            if observed.failure is None
                                            else RecipeUpdateFailure(
                                                code=str(observed.failure["code"]),
                                                detail=str(
                                                    redact_text(
                                                        str(observed.failure["detail"])
                                                    )
                                                )[:512],
                                                retryable=(
                                                    observed.failure.get("retryable")
                                                    is True
                                                ),
                                            )
                                        ),
                                        "retry_at": None,
                                        "observed_at": now,
                                    }
                                )
                            )
                    current = current.model_copy(
                        update={
                            "children": children,
                            "claim_owner": None,
                            "claim_until": None,
                            "next_attempt_at": (
                                None
                                if all(child.state in _SETTLED for child in children)
                                else now + _OBSERVATION_INTERVAL
                            ),
                        }
                    )
                    parent.payload = serialize_json_value(
                        read_update_document(serialize_json_value(current))
                    )
                    parent.state = (
                        "cancelled"
                        if all(child.state in _SETTLED for child in children)
                        else "cancelling"
                    )
                    parent.status_reason = cancellation.reason
                    parent.updated_at = now
                    progressed += 1
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    continue
                raise
        return progressed

    def activity_provider(self) -> OperationProvider:
        return OperationProvider(
            "recipe-update",
            self._activity_list,
            self._activity_get,
            represented_job_kinds=frozenset({UPDATE_KIND}),
        )

    def _activity_item(self, job: Job) -> dict[str, object]:
        from .recipe_image_availability import RecipeImageAvailabilityError

        try:
            view = self._view(job, self._document(job))
        except RecipeImageAvailabilityError as error:
            return {
                "id": job.id,
                "parent_id": None,
                "node_ids": [],
                "owner": {
                    "kind": "job",
                    "id": job.id,
                    "request_id": job.request_id,
                },
                "kind": UPDATE_KIND,
                "state": job.state,
                "attempt": job.current_attempt,
                "progress": None,
                "created_at": _now(job.created_at).isoformat(),
                "updated_at": _now(job.updated_at).isoformat(),
                "supported_actions": [],
                "status_reason": error.detail,
                "failure": {
                    "code": error.code,
                    "detail": error.detail,
                    "retryable": False,
                },
            }
        failures = sum(
            child.state in {"failed", "cancelled"} for child in view.children
        )
        return {
            "id": view.id,
            "parent_id": None,
            "node_ids": [],
            "owner": {
                "kind": "job",
                "id": job.id,
                "request_id": job.request_id,
            },
            "kind": UPDATE_KIND,
            "state": view.state,
            "attempt": view.attempt,
            "progress": serialize_json_value(view.progress),
            "created_at": view.created_at.isoformat(),
            "updated_at": view.updated_at.isoformat(),
            "supported_actions": [],
            "status_reason": f"{failures} of {len(view.children)} recipes failed or were cancelled; inspect with vonkctl recipe progress {view.id}"
            if failures
            else view.waiting_on,
        }

    def _activity_get(self, operation_id: str) -> dict[str, object]:
        with self.sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind != UPDATE_KIND:
                raise KeyError(operation_id)
            return self._activity_item(job)

    def _activity_list(self, query: OperationQuery) -> OperationListPage:
        if not 1 <= query.limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        if query.node_id is not None:
            return OperationListPage((), None, 0)
        filters = [Job.kind == UPDATE_KIND]
        if query.state is not None:
            filters.append(Job.state == query.state)
        if query.request_id is not None:
            filters.append(Job.request_id == query.request_id)
        with self.sessions() as session:
            total = (
                session.scalar(select(func.count()).select_from(Job).where(*filters))
                or 0
            )
            boundary = _activity_keyset_filter(Job.created_at, Job.id, "", query.after)
            if boundary is not None:
                filters.append(boundary)
            rows = session.scalars(
                select(Job)
                .where(*filters)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(query.limit)
            )
            return OperationListPage(
                tuple(self._activity_item(job) for job in rows), None, total
            )

    def claim(self, owner: str) -> RecipeUpdateClaim | None:
        from .recipe_image_availability import RecipeImageAvailabilityError

        if not owner or len(owner) > 95:
            raise ValueError("update worker owner must contain 1 to 95 characters")
        now = _now(self.owner._clock())
        with self.sessions() as session:
            candidates = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == UPDATE_KIND, Job.state.in_(("queued", "running"))
                    )
                    .order_by(Job.updated_at, Job.id)
                )
            )
        for operation_id in candidates:
            with self.sessions.begin() as session:
                job = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.state.in_(("queued", "running")))
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    continue
                try:
                    document = self._document(job)
                except RecipeImageAvailabilityError as error:
                    job.state = "failed"
                    job.status_reason = error.code
                    job.result = {
                        "code": error.code,
                        "detail": error.detail,
                        "retryable": False,
                    }
                    job.updated_at = now
                    continue
                if document.claim_until is not None and document.claim_until > now:
                    continue
                if (
                    document.next_attempt_at is not None
                    and document.next_attempt_at > now
                ):
                    continue
                document.claim_owner = f"{owner}:{uuid.uuid4().hex}"
                document.claim_until = now + timedelta(
                    seconds=self.owner._claim_lease_seconds
                )
                job.payload = serialize_json_value(document)
                job.state = "running"
                job.current_attempt += 1
                job.updated_at = now
                return RecipeUpdateClaim(job.id, document.claim_owner)
        return None

    def _owned(
        self, session: Session, claim: RecipeUpdateClaim
    ) -> tuple[Job, RecipeUpdateDocument]:
        job = session.scalar(
            select(Job)
            .where(Job.id == claim.operation_id, Job.kind == UPDATE_KIND)
            .with_for_update(nowait=True)
        )
        if job is None:
            raise _error(
                "recipe_update.claim_lost", "recipe update no longer owns its claim"
            )
        document = self._document(job)
        if (
            job.state != "running"
            or document.claim_owner != claim.owner
            or document.claim_until is None
            or document.claim_until <= _now(self.owner._clock())
        ):
            raise _error(
                "recipe_update.claim_lost", "recipe update no longer owns its claim"
            )
        return job, document

    def authorize_child(
        self,
        session: Session,
        claim: RecipeUpdateClaim,
        actor: str,
        request_id: str,
        intent: RecipeRevisionIntent,
    ) -> None:
        # Common order: user authority, parent Job, child request's unique key.
        # The caller's accepting transaction contains no metadata/storage I/O.
        self._authorize(session, actor)
        job, document = self._owned(session, claim)
        child = next(
            (item for item in document.children if item.request_key == request_id), None
        )
        if job.actor != actor or child is None or intent != self._intent(child):
            raise _error(
                "recipe_update.operation_invalid",
                "child admission does not match the accepted update scope",
            )
        revision = session.get(CatalogDocumentRevision, child.recipe_revision_id)
        if (
            revision is None
            or revision.content_digest != child.recipe_content_sha256
            or revision.execution_key != child.effective_execution_key
        ):
            raise _error(
                "recipe_update.operation_invalid",
                "child recipe no longer matches the accepted update identity",
            )

    @staticmethod
    def _intent(child: RecipeUpdateChild) -> RecipeRevisionIntent:
        return RecipeRevisionIntent(
            recipe_revision_id=child.recipe_revision_id,
            effective_execution_key=child.effective_execution_key,
            force=True,
        )

    def run(self, claim: RecipeUpdateClaim) -> None:
        from .recipe_image_availability import (
            RecipeImageAvailabilityError,
            RecipeImageAvailabilityView,
            _retryable,
        )

        with self.sessions.begin() as session:
            job, document = self._owned(session, claim)
            actor = job.actor
        now = _now(self.owner._clock())
        eligible = [
            index
            for index, child in enumerate(document.children)
            if child.state not in _SETTLED
            and (child.retry_at is None or child.retry_at <= now)
        ]
        if eligible:
            index = next(
                (item for item in eligible if item >= document.next_child), eligible[0]
            )
            child = document.children[index]
            try:
                try:
                    observed = self.owner.get_operator_request(
                        child.request_key, actor=actor
                    )
                except KeyError:
                    observed = self.owner._start_request(
                        self._intent(child),
                        actor=actor,
                        request_id=child.request_key,
                        update_claim=claim,
                    )
                if (
                    not isinstance(observed, RecipeImageAvailabilityView)
                    or observed.request != self._intent(child)
                    or observed.recipe_content_sha256 != child.recipe_content_sha256
                    or (
                        child.operation_id is not None
                        and child.operation_id != observed.id
                    )
                ):
                    raise _error(
                        "recipe_update.operation_invalid",
                        "child receipt does not match its frozen recipe identity",
                    )
                child.operation_id = observed.id
                child.state = cast(UpdateState, observed.state)
                child.failure = (
                    None
                    if observed.failure is None
                    else RecipeUpdateFailure(
                        code=str(observed.failure["code"]),
                        detail=str(redact_text(str(observed.failure["detail"])))[:512],
                        retryable=observed.failure.get("retryable") is True,
                    )
                )
                child.retry_at = None
            except RecipeImageAvailabilityError as error:
                if error.code == "recipe_update.claim_lost":
                    return
                retryable = _retryable(error)
                child.failure = RecipeUpdateFailure(
                    code=error.code,
                    detail=str(redact_text(error.detail))[:512],
                    retryable=retryable,
                )
                child.state = "pending" if retryable else "failed"
                child.retry_at = (
                    now + timedelta(seconds=max(2, error.retry_after_seconds or 2))
                    if retryable
                    else None
                )
            except (ValueError, TypeError):
                child.state = "failed"
                child.failure = RecipeUpdateFailure(
                    code="recipe_update.observation_invalid",
                    detail="child operation returned invalid persisted evidence",
                    retryable=False,
                )
                child.retry_at = None
            child.observed_at = now
            document.next_child = (index + 1) % len(document.children)
        with self.sessions.begin() as session:
            job, _ = self._owned(session, claim)
            states = [child.state for child in document.children]
            if all(state in _SETTLED for state in states):
                job.state = (
                    "succeeded"
                    if all(state == "succeeded" for state in states)
                    else ("partial" if "succeeded" in states else "failed")
                )
                document.next_attempt_at = None
            else:
                job.state = (
                    "running"
                    if any(
                        child.operation_id is not None for child in document.children
                    )
                    else "queued"
                )
                document.next_attempt_at = min(
                    child.retry_at
                    or (
                        now if child.state == "pending" else now + _OBSERVATION_INTERVAL
                    )
                    for child in document.children
                    if child.state not in _SETTLED
                )
            document.claim_owner = document.claim_until = None
            # Validate the persisted current contract, including JSON-mode fields.
            job.payload = serialize_json_value(
                read_update_document(serialize_json_value(document))
            )
            job.updated_at = _now(self.owner._clock())
