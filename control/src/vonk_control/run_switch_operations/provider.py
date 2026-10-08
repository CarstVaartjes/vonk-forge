"""Provider."""

from __future__ import annotations

import re
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)

from pydantic import ValidationError
from sqlalchemy import String, cast, func, select
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
)

from .. import job_states
from ..categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from ..lifecycle.run_switch import (
    RunSwitchAdapter,
)
from ..models import (
    Job,
)
from ..operation_api import (
    OperationListPage,
    OperationQuery,
    _activity_keyset_filter,
)
from ..operation_item_contract import OperationItem
from ..run_switch_contract import (
    RunSwitchOperation,
)
from ..strict_json import (
    warn_unreadable_once,
)
from .activity import _activity_progress, _activity_result
from .constants import _OPERATION_KINDS
from .errors import RunSwitchOperationConflict
from .planning_helpers import _aware, _stored_job_plan

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


_ADAPTER = RunSwitchAdapter()


class RunSwitchOperationProvider:
    """Project high-level jobs into the global Activity provider contract."""

    family = "run-switch"

    def __init__(self, service: RunSwitchOperationService) -> None:
        self._service = service

    @property
    def represented_job_kinds(self) -> frozenset[str]:
        return _OPERATION_KINDS

    def list_operations(self, query: OperationQuery) -> OperationListPage:
        limit = query.limit
        if type(limit) is not int or not 1 <= limit <= 101:
            raise InvalidValue(
                "operation provider page limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        state = query.state
        if state is not None and not isinstance(state, str):
            raise InvalidValue("operation provider state filter is invalid")
        node_id = query.node_id
        if node_id is not None and not isinstance(node_id, str):
            raise InvalidValue("operation provider node filter is invalid")
        request_id = query.request_id
        if request_id is not None and not isinstance(request_id, str):
            raise InvalidValue("operation provider request filter is invalid")
        after = query.after
        with self._service._sessions() as session:
            base_filters: list[ColumnElement[bool]] = [Job.kind.in_(_OPERATION_KINDS)]
            if state is not None:
                base_filters.append(Job.state == state)
            if request_id is not None:
                base_filters.append(Job.request_id == request_id)
            if node_id is not None:
                base_filters.append(cast(Job.targets, String).contains(f'"{node_id}"'))
            if after is not None and (
                not isinstance(after, tuple)
                or len(after) != 2
                or not isinstance(after[0], datetime)
                or not isinstance(after[1], str)
            ):
                raise InvalidValue("operation provider cursor is invalid")
            filters = list(base_filters)
            boundary = _activity_keyset_filter(
                Job.created_at,
                Job.id,
                "",
                None if after is None else (_aware(after[0]), after[1]),
            )
            if boundary is not None:
                filters.append(boundary)
            total = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(*base_filters)
                )
                or 0
            )
            jobs = list(
                session.scalars(
                    select(Job)
                    .where(*filters)
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(limit)
                )
            )
            items = tuple(self._item(job) for job in jobs[:limit])
        return OperationListPage(items, None, total)

    def get_operation(self, operation_id: str) -> OperationItem:
        with self._service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise MissingRecord(operation_id)
            return self._item(job)

    def _item(self, job: Job) -> OperationItem:
        node_ids: list[str] = []
        # Malformed stored targets read as an unreadable row (shown below), the
        # same as a plan that does not parse.
        readable = not (
            not isinstance(job.targets, list)
            or not job.targets
            or any(
                not isinstance(node_id, str)
                or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
                for node_id in job.targets
            )
        )
        operation: RunSwitchOperation | None = None
        if readable:
            node_ids = list(job.targets)
            try:
                operation = self._service._operation_view(job)
            except (
                AttributeError,
                KeyError,
                RunSwitchOperationConflict,
                TypeError,
                ValueError,
                ValidationError,
            ):
                operation = None
        if operation is None:
            warn_unreadable_once("run-switch job", job.id)
            return OperationItem.model_validate(
                {
                    "id": job.id,
                    "job_id": job.id,
                    "parent_id": None,
                    "owner": {
                        "kind": "job",
                        "id": job.id,
                        "request_id": job.request_id,
                    },
                    # Job.targets is the durable owner of target membership. Keep
                    # a valid scope visible when plan parsing fails so global
                    # node-filtered Activity pages retain this unreadable row.
                    "node_ids": node_ids,
                    "kind": "run-switch-unreadable",
                    "state": "unavailable",
                    "attempt": max(0, job.current_attempt or 0),
                    "progress": None,
                    "created_at": _aware(job.created_at).isoformat(),
                    "updated_at": _aware(job.updated_at).isoformat(),
                    "supported_actions": [],
                    "status_reason": "Stored Run/Switch history is unreadable.",
                }
            )
        endpoint_node = node_ids[0]
        plan = _stored_job_plan(job)
        if plan is not None:
            endpoint_node = next(
                (
                    node.node_id
                    for node in plan.spark_group.nodes
                    if node.endpoint_owner
                ),
                endpoint_node,
            )
        return OperationItem.model_validate(
            {
                "id": operation.operation_id,
                "job_id": operation.operation_id,
                "parent_id": None,
                "owner": {
                    "kind": "job",
                    "id": job.id,
                    "request_id": job.request_id,
                },
                # The singular field is retained for older Activity readers; the
                # complete group is authoritative in node_ids and progress.members.
                "node_id": endpoint_node,
                "node_ids": node_ids,
                "kind": operation.kind,
                "state": operation.state,
                "attempt": max(1, job.current_attempt or 0),
                "progress": _activity_progress(operation),
                "created_at": _aware(job.created_at).isoformat(),
                "updated_at": _aware(job.updated_at).isoformat(),
                "supported_actions": (
                    ["retry"]
                    if operation.state == LifecycleState.FAILED.value
                    and operation.result
                    and operation.result.retryable
                    else ["cancel"]
                    if operation.state
                    in (
                        *job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.OBSERVING,
                        ),
                        "unknown",
                    )
                    and not (operation.result and operation.result.cancellation)
                    and (
                        operation.state == LifecycleState.QUEUED.value
                        or operation.current_phase not in {"start", "final_verify"}
                    )
                    else []
                ),
                "result": _activity_result(operation),
                "detail": operation.status_reason,
                "blockers": [
                    item.model_dump(mode="json") for item in operation.blockers
                ],
                "next_attempt_at": (
                    operation.next_attempt_at.isoformat()
                    if operation.next_attempt_at is not None
                    else None
                ),
            }
        )
