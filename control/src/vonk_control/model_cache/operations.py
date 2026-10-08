"""Operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, ModelCacheCode

from .. import model_cache_states
from ..categorized_errors import InvalidValue
from ..lifecycle import State
from ..logging import redact_text
from ..model_cache_contract import (
    ModelCacheOperationResponse,
    ModelCacheOperatorAction,
    ModelCacheRemovalPayload,
)
from ..models import CatalogDocumentRevision, ModelCacheOperation, ModelCacheSet
from ..operation_contract import AvailabilityOperationFailure
from ..strict_json import serialize_json_value
from .catalog_helpers import _datetime, _iso, _parse_iso
from .constants import SCHEMA_VERSION
from .errors import ModelCacheConflictInvalid, ModelCacheNotFoundInvalid
from .persistence import _cache_failure, _derived_result, _operation_progress
from .source_helpers import _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class OperationsMixin:
    """Operations behavior of the cache service."""

    def get_operation(self, operation_id: str) -> CacheOperationView:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            return cache._operation_view(operation)

    def get_operator_operation(
        self, operation_id: str
    ) -> tuple[CacheOperationView, ModelCacheOperatorAction, str]:
        """Return one model operation with its action and a selector.

        A read never refuses for a missing optional fact: an operation the
        platform started itself (a recipe preparation, a repair) carries no
        operator selector, so the model's catalog name, its content digest, or
        at last the operation id stands in. All of them name the operation.
        """
        cache = cast("ModelCacheService", self)

        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            payload = cache._payload_or_none(operation)
            selector = None if payload is None else payload.selector
            if not isinstance(selector, str) or not selector:
                model_digest = (
                    payload.model_content_sha256
                    if isinstance(payload, ModelCacheRemovalPayload)
                    else payload.manifest.model_content_sha256
                    if payload is not None
                    else None
                )
                selector = cache._observed_selector(
                    session,
                    operation,
                    model_digest if isinstance(model_digest, str) else None,
                )
            action: ModelCacheOperatorAction = (
                "remove" if operation.kind == "remove" else "download"
            )
            return cache._operation_view(operation), action, selector

    @staticmethod
    def _observed_selector(
        session: Session,
        operation: ModelCacheOperation,
        model_content_sha256: str | None,
    ) -> str:
        """Name a selector-less operation by its model, else its id."""

        digest = model_content_sha256
        if not isinstance(digest, str) or not digest:
            digest = session.scalar(
                select(ModelCacheSet.model_content_sha256).where(
                    ModelCacheSet.artifact_set_sha256 == operation.artifact_set_sha256
                )
            )
        if isinstance(digest, str) and digest:
            row = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.content_digest == digest,
                )
                .limit(1)
            )
            if row is not None and row.publisher and row.slug:
                return f"{row.publisher}/{row.slug}"[:256]
            return digest
        return operation.id

    def get_operator_request(
        self, request_key: str, *, actor: str
    ) -> tuple[CacheOperationView, ModelCacheOperatorAction, str]:
        """Resolve an operator key without exposing another issuer's binding."""
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        with cache._session() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key,
                    ModelCacheOperation.actor == actor,
                )
            )
            if operation is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            operation_id = operation.id
        return cache.get_operator_operation(operation_id)

    def list_operations(self, *, limit: int = 100) -> tuple[CacheOperationView, ...]:
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 100:
            raise InvalidValue("cache operation limit is invalid")
        with cache._session() as session:
            rows = session.scalars(
                select(ModelCacheOperation)
                .order_by(
                    ModelCacheOperation.created_at.desc(), ModelCacheOperation.id.desc()
                )
                .limit(limit)
            )
            return tuple(cache._operation_view(row) for row in rows)

    def operations_page(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a stable created-at/id ordered page and raw next boundary."""
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 100:
            raise InvalidValue("cache operation limit is invalid")
        with cache._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheOperation).order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if _datetime(row.created_at) == boundary_time and row.id == boundary[1]:
                    start = index + 1
                    break
            else:
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.CURSOR_INVALID, "operation cursor boundary is stale"
                )
        page = rows[start : start + limit]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "schema_version": SCHEMA_VERSION,
            "operations": tuple(cache._operation_view(row) for row in page),
            "total": total,
            "_next_boundary": next_boundary,
        }

    @staticmethod
    def _operation_view(operation: ModelCacheOperation) -> CacheOperationView:
        # An unreadable document renders from the row's own columns: a succeeded
        # operation with its derived result, a failed one with its ``last_error``
        # as the failure evidence.  Reading never raises on a damaged envelope,
        # progress or result (a damaged ``cancellation`` sub-document still does:
        # that raise is the input-validation family's).
        from .service import ModelCacheService

        stored = ModelCacheService._payload_of(operation)
        progress = _operation_progress(operation)
        cancellation = None if stored is None else stored.cancellation
        result = None if stored is None else stored.result
        failure = None if stored is None else stored.failure
        if stored is None:
            result = _derived_result(operation)
            if operation.state == State.FAILED and failure is None:
                failure = _cache_failure(
                    ModelCacheCode.DOCUMENT_UNREADABLE,
                    redact_text(
                        operation.last_error or "operation document is unreadable"
                    )[:512],
                    retryable=False,
                    recovery="inspect",
                )
        removal = stored if isinstance(stored, ModelCacheRemovalPayload) else None
        waiting = operation.state in model_cache_states.WAITING_OR_FAILED
        blockers = tuple(stored.blockers) if stored is not None and waiting else ()
        # The one retry clock is the column; a row written before the core still
        # carries it in the payload until its next transition.
        next_attempt = (
            _iso(operation.next_action_at)
            if blockers and operation.state in model_cache_states.WAITING
            else None
        )
        view = CacheOperationView(
            id=operation.id,
            request_key=operation.request_key,
            kind=operation.kind,
            state=(
                LifecycleState.OBSERVING.value
                if cancellation is not None and operation.state != "cancelled"
                else model_cache_states.adopted(operation.state)
            ),
            attempt=int(operation.attempt),
            model_content_sha256=None
            if removal is None
            else removal.model_content_sha256,
            artifact_set_sha256=operation.artifact_set_sha256,
            plan_digest=operation.plan_digest,
            review_digest=None if removal is None else removal.review_digest,
            progress=serialize_json_value(progress),  # type: ignore[arg-type]
            result=result,
            last_error=operation.last_error,
            created_at=_iso(operation.created_at) or "",
            updated_at=_iso(operation.updated_at) or "",
            completed_at=_iso(operation.completed_at),
            retryable=failure is not None and failure.retryable,
            failure=None if failure is None else failure.model_dump(mode="json"),
            cancellation=(
                None if cancellation is None else cancellation.model_dump(mode="json")
            ),
            blockers=blockers,
            next_attempt_at=next_attempt if isinstance(next_attempt, str) else None,
        )
        ModelCacheOperationResponse.model_validate(view, from_attributes=True)
        return view

    @staticmethod
    def _canonical_failure(
        operation: ModelCacheOperation,
    ) -> AvailabilityOperationFailure | None:
        """Read the one current persisted failure contract without repair/defaults."""
        from .service import ModelCacheService

        payload = ModelCacheService._payload_of(operation)
        return None if payload is None else payload.failure
