"""Operation Api: providers."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ConfigDict, StrictStr, TypeAdapter, ValidationError
from sqlalchemy import String, and_, cast, false, func, or_, select, true
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression
from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    SecurityRefusalError,
    canonical_message,
)

from ..auth import CursorCodec
from ..models import AgentOperation, Job
from ..operation_blockers import read_blockers
from ..operation_item_contract import (
    OperationItem,
    OperationOwnerReference,
    operation_item,
)
from ..state_filters import state_filter
from ..strict_json import warn_unreadable_once
from .constants import NODE_PATTERN
from .contracts import (
    OperationApiServices,
    OperationListPage,
    OperationProjectionError,
    OperationProvider,
    OperationProviderProtocol,
    OperationQuery,
)
from .diagnostics import _aware

# Observation cursors confer no mutation authority. An otherwise standalone
# provider still needs authenticated pagination when no application codec was
# supplied; its process-local key expires on restart, like the observation.
_OBSERVATION_CURSORS = CursorCodec(secrets.token_bytes(32))


def observation_cursors() -> CursorCodec:
    return _OBSERVATION_CURSORS


def _operation_boundary(item: OperationItem) -> tuple[datetime, str] | None:
    """Missing local boundary evidence cannot authenticate a continuation."""
    if item.created_at is None or not item.id:
        return None
    try:
        parsed = datetime.fromisoformat(item.created_at)
    except (TypeError, ValueError):
        return None
    return _aware(parsed), item.id


def merge_operation_providers(
    providers: Sequence[OperationProviderProtocol],
    *,
    cursor: str | None,
    limit: int,
    state: str | None,
    node_id: str | None,
    request_id: str | None = None,
    cursors: CursorCodec,
    now: datetime | None = None,
) -> OperationListPage:
    """Merge provider rows using one deterministic newest-first cursor."""

    if not 1 <= limit <= 100:
        raise ValueError("operation page limit is invalid")
    context = {"state": state, "node_id": node_id, "request_id": request_id}
    after: tuple[datetime, str] | None = None
    if cursor is not None:
        decoded = cursors.decode(
            cursor,
            resource="operations",
            order="created-at-desc/id-desc/v1",
            context=context,
        )
        # The codec authenticated the token. Unreadable issued bookkeeping
        # restarts this finite observation against the current providers.
        if (
            isinstance(decoded, list)
            and len(decoded) == 2
            and all(isinstance(item, str) for item in decoded)
        ):
            try:
                after = (_aware(datetime.fromisoformat(decoded[0])), decoded[1])
            except ValueError:
                after = None
    query = OperationQuery(
        after=after,
        limit=limit + 1,
        state=state,
        node_id=node_id,
        request_id=request_id,
        projected_at=now,
    )
    rows: list[OperationItem] = []
    total: int | None = 0
    seen: set[str] = set()
    projection_issue = None
    for provider in providers:
        page = None
        for _attempt in range(3):
            try:
                candidate = provider.list_operations(query)
                page = candidate
                if not candidate.continuation_unavailable and all(
                    _operation_boundary(operation_item(row)) is not None
                    for row in candidate.items
                ):
                    break
            except SecurityRefusalError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
        if page is None:
            projection_issue = "Some operation observations are unavailable."
            total = None
            continue
        if total is not None:
            if type(page.total) is int and page.total >= 0:
                total += page.total
            else:
                total = None
        projection_issue = projection_issue or page.projection_issue
        if page.continuation_unavailable:
            projection_issue = "Some operation observations are unavailable."
        for row in page.items:
            try:
                item = operation_item(row)
                node_ids = item.node_ids
                if not all(re.fullmatch(NODE_PATTERN, node) for node in node_ids):
                    raise ValueError("provider node observation is unavailable")
                boundary = _operation_boundary(item)
                if boundary is None:
                    projection_issue = "Some operation observations are unavailable."
                    continue
            except (TypeError, ValueError, OperationProjectionError):
                warn_unreadable_once(provider.family, str(getattr(row, "id", "row")))
                projection_issue = "Some operation observations are unavailable."
                continue
            if node_id is not None and node_id not in node_ids:
                continue
            if after is not None and boundary >= after:
                projection_issue = "Some operation observations are unavailable."
                continue
            operation_id = boundary[1]
            if operation_id in seen:
                for index, previous in enumerate(rows):
                    if previous.id == operation_id and canonical_message(
                        previous
                    ) != canonical_message(item):
                        try:
                            reconciled = get_operation_from_providers(
                                providers, operation_id, now=now
                            )
                        except KeyError:
                            reconciled = OperationItem(
                                id=operation_id,
                                kind=previous.kind,
                                state=LifecycleState.OBSERVING.value,
                                attempt=0,
                                result_unreadable=True,
                                supported_actions=[],
                            )
                        rows[index] = reconciled
                        if reconciled.result_unreadable:
                            projection_issue = (
                                "Some operation observations are unavailable."
                            )
                continue
            seen.add(operation_id)
            rows.append(item)
    rows.sort(
        key=lambda row: (
            _operation_boundary(row) or (datetime.min.replace(tzinfo=UTC), "")
        ),
        reverse=True,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        boundary = _operation_boundary(rows[-1])
        if boundary is None:
            projection_issue = "Some operation observations are unavailable."
        else:
            created_at, operation_id = boundary
            next_cursor = cursors.encode(
                resource="operations",
                order="created-at-desc/id-desc/v1",
                context=context,
                boundary=[created_at.isoformat(), operation_id],
            )
    return OperationListPage(
        items=rows,
        next_cursor=next_cursor,
        total=total,
        projection_issue=projection_issue,
        continuation_unavailable=projection_issue is not None,
    )


def get_operation_from_providers(
    providers: Sequence[OperationProviderProtocol],
    operation_id: str,
    *,
    now: datetime | None = None,
) -> OperationItem:
    """Resolve one operation without coupling the Controller to provider modules."""

    # Reconcile the whole provider set, not each provider against a historical
    # first reply. A disagreement can clear only when both owners are observed
    # again. Three complete passes bound this read; no execution claim is held.
    matches: list[OperationItem] = []
    unavailable = False
    for _attempt in range(3):
        matches = []
        unavailable = False
        for provider in providers:
            try:
                raw = (
                    provider.get_operation_at(operation_id, now)
                    if now is not None
                    and isinstance(provider, OperationProvider)
                    and provider.get_operation_at is not None
                    else provider.get_operation(operation_id)
                )
                candidate = operation_item(raw)
                if candidate.id != operation_id:
                    unavailable = True
                    continue
                matches.append(candidate)
                unavailable |= _operation_boundary(candidate) is None
            except KeyError:
                continue
            except SecurityRefusalError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                unavailable = True
        if not unavailable and (
            not matches
            or all(
                canonical_message(item) == canonical_message(matches[0])
                for item in matches[1:]
            )
        ):
            if matches:
                return matches[0]
            raise KeyError(operation_id)
    if not matches:
        return OperationItem(
            id=operation_id,
            kind=providers[0].family,
            state=LifecycleState.OBSERVING.value,
            attempt=0,
            result_unreadable=True,
            supported_actions=[],
        )
    match = matches[0]
    if all(canonical_message(item) == canonical_message(match) for item in matches[1:]):
        # A missing timestamp/unknown sibling does not change known run state.
        return match.model_copy(
            update={"result_unreadable": True, "supported_actions": []}
        )
    # No provider wins by iteration order. Keep only agreed identity/state facts
    # and expose no action, receipt, owner or progress from disputed evidence.
    return OperationItem(
        id=operation_id,
        kind=match.kind
        if all(item.kind == match.kind for item in matches)
        else providers[0].family,
        state=match.state
        if all(item.state == match.state for item in matches)
        else LifecycleState.OBSERVING.value,
        attempt=min(item.attempt for item in matches),
        node_ids=sorted(set.intersection(*(set(item.node_ids) for item in matches))),
        created_at=match.created_at
        if all(item.created_at == match.created_at for item in matches)
        else None,
        updated_at=match.updated_at
        if all(item.updated_at == match.updated_at for item in matches)
        else None,
        result_unreadable=True,
        supported_actions=[],
    )


_ACTIVITY_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ACTIVITY_TARGET = re.compile(
    r"^(?:[a-z0-9][a-z0-9._-]{0,62}|"
    r"[a-z0-9][a-z0-9._-]{0,62}/[a-z0-9][a-z0-9._-]{0,62})$"
)
_ACTIVITY_STRINGS = TypeAdapter(list[StrictStr], config=ConfigDict(strict=True))
_JOB_ACTIVITY_PREFIX = "job:"


def _activity_keyset_filter(
    created_at_column: SQLColumnExpression[datetime],
    id_column: SQLColumnExpression[str],
    id_prefix: str,
    after: tuple[datetime, str] | None,
) -> ColumnElement[bool] | None:
    """Build the provider-local half of the shared prefixed ID boundary."""

    if after is None:
        return None
    created_at, activity_id = after
    if activity_id.startswith(id_prefix):
        same_time_ids = id_column < activity_id[len(id_prefix) :]
    elif id_prefix < activity_id:
        same_time_ids = true()
    else:
        same_time_ids = false()
    return or_(
        created_at_column < created_at,
        and_(created_at_column == created_at, same_time_ids),
    )


def _activity_node_ids(value: object, *, limit: int) -> list[str]:
    """Validate decoded JSON like stored JSON, then select exact Spark targets."""

    if (
        not isinstance(value, (list, tuple))
        or len(value) > limit
        or any(not isinstance(target, str) or len(target) > 127 for target in value)
    ):
        raise ValueError("stored activity targets are malformed")
    try:
        values = _ACTIVITY_STRINGS.validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise ValueError("stored activity targets are malformed") from error
    if len(values) > limit or any(
        len(target) > 127 or _ACTIVITY_TARGET.fullmatch(target) is None
        for target in values
    ):
        raise ValueError("stored activity targets are malformed")
    return [
        target for target in values if re.fullmatch(NODE_PATTERN, target) is not None
    ]


def _activity_owner_request_id(value: object) -> str | None:
    if isinstance(value, str) and _ACTIVITY_REQUEST_ID.fullmatch(value) is not None:
        return value
    return None


class _StandaloneJobActivityProjection:
    """Project Jobs not already represented by an exact operation owner."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        providers: Sequence[OperationProviderProtocol],
    ) -> None:
        self._sessions = sessions
        self._represented_job_kinds = frozenset(
            kind
            for provider in providers
            for kind in getattr(provider, "represented_job_kinds", frozenset())
        )

    def _base_filters(self, query: OperationQuery) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = []
        if self._represented_job_kinds:
            filters.append(Job.kind.not_in(self._represented_job_kinds))
        filters.append(
            ~select(AgentOperation.id)
            .where(AgentOperation.parent_job_id == Job.id)
            .exists()
        )
        if query.state is not None:
            filters.append(state_filter(Job.state, LifecycleSubject.JOB, query.state))
        if query.request_id is not None:
            filters.append(Job.request_id == query.request_id)
        if query.node_id is not None:
            filters.append(cast(Job.targets, String).contains(f'"{query.node_id}"'))
        return filters

    def list_operations(self, query: OperationQuery) -> OperationListPage:
        if not 1 <= query.limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        base_filters = self._base_filters(query)
        page_filters = list(base_filters)
        boundary = _activity_keyset_filter(
            Job.created_at, Job.id, _JOB_ACTIVITY_PREFIX, query.after
        )
        if boundary is not None:
            page_filters.append(boundary)
        with self._sessions() as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(*base_filters)
                )
                or 0
            )
            jobs = session.scalars(
                select(Job)
                .where(*page_filters)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(query.limit)
            )
            return OperationListPage(
                tuple(self._item(job) for job in jobs), None, total
            )

    def get_operation(self, operation_id: str) -> OperationItem:
        if not operation_id.startswith(_JOB_ACTIVITY_PREFIX):
            raise KeyError(operation_id)
        owner_id = operation_id[len(_JOB_ACTIVITY_PREFIX) :]
        with self._sessions() as session:
            job = session.get(Job, owner_id)
            if job is None or not self._is_standalone(session, job):
                raise KeyError(operation_id)
            return self._item(job)

    def _is_standalone(self, session: Session, job: Job) -> bool:
        if job.kind in self._represented_job_kinds:
            return False
        return (
            session.scalar(
                select(AgentOperation.id)
                .where(AgentOperation.parent_job_id == job.id)
                .limit(1)
            )
            is None
        )

    def _item(self, job: Job) -> OperationItem:
        activity_id = f"{_JOB_ACTIVITY_PREFIX}{job.id}"
        request_id = _activity_owner_request_id(job.request_id)
        try:
            if (
                not isinstance(job.id, str)
                or not 1 <= len(activity_id) <= 128
                or request_id is None
                or not isinstance(job.kind, str)
                or not 1 <= len(job.kind) <= 80
                or not isinstance(job.state, str)
                or not 1 <= len(job.state) <= 80
                or type(job.current_attempt) is not int
                or job.current_attempt < 0
                or not isinstance(job.created_at, datetime)
                or not isinstance(job.updated_at, datetime)
            ):
                raise ValueError("stored job activity is malformed")
            node_ids = _activity_node_ids(job.targets, limit=100)
            status_reason = job.status_reason
            if status_reason is not None and (
                not isinstance(status_reason, str) or len(status_reason) > 1024
            ):
                raise ValueError("stored job status reason is malformed")
        except (AttributeError, TypeError, ValueError, ValidationError):
            warn_unreadable_once("job", job.id)
            return self._unreadable_item(job, activity_id, request_id)
        return OperationItem(
            id=activity_id,
            job_id=job.id,
            owner=OperationOwnerReference(kind="job", id=job.id, request_id=request_id),
            node_ids=node_ids,
            kind=job.kind,
            state=job.state,
            attempt=job.current_attempt,
            created_at=_aware(job.created_at).isoformat(),
            updated_at=_aware(job.updated_at).isoformat(),
            supported_actions=[],
            status_reason=status_reason,
            blockers=(
                read_blockers(job.payload.get("blockers"))
                if isinstance(job.payload, Mapping)
                else None
            ),
            next_attempt_at=(
                _text_or_none(job.payload.get("retry_after_at"))
                if isinstance(job.payload, Mapping) and job.state == "queued"
                else None
            ),
        )

    def _unreadable_item(
        self, job: Job, activity_id: str, request_id: str | None
    ) -> OperationItem:
        return OperationItem(
            id=activity_id,
            job_id=job.id,
            owner=OperationOwnerReference(kind="job", id=job.id, request_id=request_id),
            kind="job-history-unreadable",
            state="unavailable",
            attempt=0,
            created_at=_aware(job.created_at).isoformat(),
            updated_at=_aware(job.updated_at).isoformat(),
            supported_actions=[],
            status_reason="Stored job history is unreadable.",
        )


def _global_list_operations(
    services: OperationApiServices,
    cursor: str | None,
    limit: int,
    state: str | None,
    node_id: str | None,
    request_id: str | None,
    *,
    now: datetime | None = None,
) -> OperationListPage:
    if services.operation_providers:
        return merge_operation_providers(
            services.operation_providers,
            cursor=cursor,
            limit=limit,
            state=state,
            node_id=node_id,
            request_id=request_id,
            cursors=services.cursor_codec or observation_cursors(),
            now=now,
        )
    if services.list_operations is None:
        return OperationListPage(items=(), next_cursor=None, total=0)
    return services.list_operations(cursor, limit, state, node_id, request_id)


def _global_get_operation(
    services: OperationApiServices, operation_id: str, *, now: datetime | None = None
) -> OperationItem:
    if services.operation_providers:
        return get_operation_from_providers(
            services.operation_providers, operation_id, now=now
        )
    if services.get_operation is None:
        raise KeyError(operation_id)
    return operation_item(services.get_operation(operation_id))


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
