"""One-time conversion of retained schema-2 single-child profile journals.

This module is never an execution reader: it proves the old journal's identities
against accepted SQL children, then writes the sole current progress contract.
Unprovable journals remain intact and are revisited with a bounded backoff.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfilePreview,
    FleetProfileReviewedDecision,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildKind,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchChildState,
    FleetProfileSwitchPendingChild,
    UuidId,
    profile_switch_child_request_key,
)
from .lifecycle.evidence import BookkeepingReason
from .models import FleetProfileApplication, FleetProfileSelection, Job, RecipeRun
from .run_switch_contract import RunSwitchOperationResult, RunSwitchPlan
from .strict_json import StrictModel

_LOGGER = logging.getLogger(__name__)
_DEFERRED = "Profile journal conversion waiting: "
_RETRY_INTERVAL = timedelta(seconds=30)
_SCAN_SECONDS = 1.0
_SCAN_BYTES = 4 * 1024 * 1024
# A bounded SQL page limits locks; the byte/time budgets bound work after claim.
_PAGE_ROWS = 16


class ProfileAdapterConversionOutcome(StrictModel):
    state: Literal["current", "converted", "deferred"]
    reason: BookkeepingReason | None = None
    next_attempt_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=512)


class _RetainedChild(StrictModel):
    operation_id: UuidId
    kind: FleetProfileSwitchChildKind
    state: Literal["succeeded", "failed", "cancelled"]
    result: FleetProfileSwitchChildResult | None = None


class _UnprovenJournal(ValueError):
    pass


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def needs_conversion(row: FleetProfileApplication) -> bool:
    """Detect only the retired journal encoding, without interpreting its effects."""
    if not isinstance(row.progress, dict):
        return False
    adapter = row.progress.get("switch_adapter")
    return isinstance(adapter, dict) and (
        "active_operation_id" in adapter or "active_kind" in adapter
    )


def conversion_observation(
    row: FleetProfileApplication,
) -> ProfileAdapterConversionOutcome:
    if not needs_conversion(row):
        return ProfileAdapterConversionOutcome(state="current")
    return ProfileAdapterConversionOutcome(
        state="deferred",
        reason=BookkeepingReason.PERSISTED_STATE_DAMAGED,
        next_attempt_at=_aware(row.updated_at) + _RETRY_INTERVAL
        if (row.status_reason or "").startswith(_DEFERRED)
        else _aware(row.updated_at),
        detail=row.status_reason
        if (row.status_reason or "").startswith(_DEFERRED)
        else "Retained schema-2 journal awaits exact accepted child/queue proof",
    )


def _job_proof(
    session: Session,
    row: FleetProfileApplication,
    state: FleetProfileSwitchAdapterState,
    progress: FleetProfileApplicationProgress,
    reviewed: FleetProfilePreview,
    index: int,
    operation_id: str,
) -> Job:
    # Imports stay here to keep startup independent of the orchestration module.
    from .fleet_profiles import FleetProfileReviewStale, RunSwitchFleetProfileAdapter
    from .run_switch_operations import _digest

    item = state.queue[index]
    job = session.get(Job, operation_id)
    if job is None:
        raise _UnprovenJournal("exact child row is unavailable")
    expected_key = profile_switch_child_request_key(row.id, index, item.kind, item.id)
    expected_kind = {
        "run": "recipe.run-switch.v2",
        "install": "recipe.run-switch.v2",
        "stop": "recipe.stop.v2",
        "cleanup": "recipe.cleanup.v2",
    }[item.kind]
    if job.request_id != expected_key or job.kind != expected_kind:
        raise _UnprovenJournal(
            "exact child request or kind differs from its queue index"
        )
    if job.payload_digest != _digest(job.payload):
        raise _UnprovenJournal("accepted child payload integrity differs from SQL")
    if (
        progress.workload_intent_ordinal is None
        or job.payload.get("workload_intent_ordinal")
        != progress.workload_intent_ordinal
    ):
        raise _UnprovenJournal(
            "exact child workload intent is unavailable or different"
        )
    plan = RunSwitchPlan.model_validate_json(
        canonical_message(job.payload.get("plan")), strict=True
    )
    targets = tuple(
        sorted(
            plan.profile_stop_scope.target_node_ids
            if plan.profile_stop_scope is not None
            else [node.node_id for node in plan.spark_group.nodes]
        )
    )
    if targets != tuple(sorted(job.targets)) or not set(targets) <= set(
        state.scope_node_ids
    ):
        raise _UnprovenJournal(
            "exact child target scope differs from its accepted journal"
        )
    try:
        RunSwitchFleetProfileAdapter._validate_child_effects(
            reviewed, plan, execution_scope=targets
        )
    except FleetProfileReviewStale as error:
        raise _UnprovenJournal("child effects differ from accepted consent") from error
    if item.kind == "stop":
        valid = (
            plan.action == "stop"
            and plan.run_id == item.id
            and plan.profile_stop_scope == item.profile_stop_scope
        )
        effect = next(
            (
                effect
                for effect in reviewed.effects.runs
                if effect.run_id == item.id and effect.action == "stop"
            ),
            None,
        )
        valid = (
            valid
            and effect is not None
            and effect.installation_id == plan.installation_id
        )
    elif item.kind == "cleanup":
        valid = plan.action == "cleanup" and plan.installation_id == item.id
    else:
        assignment = next(
            (
                assignment
                for assignment in state.assignments
                if assignment.id == item.id
            ),
            None,
        )
        receipt = RunSwitchOperationResult.model_validate_json(
            canonical_message(job.result), strict=True
        )
        valid = assignment is not None and receipt.profile_application_id == row.id
        if assignment is not None:
            valid = valid and (
                plan.action,
                plan.alias,
            ) == RunSwitchFleetProfileAdapter._assignment_intent(assignment)
            valid = valid and plan.recipe_revision_id == assignment.recipe_revision_id
            valid = valid and plan.spark_group.nodes == sorted(
                assignment.nodes, key=lambda node: node.rank
            )
    if not valid:
        raise _UnprovenJournal("exact child differs from the accepted effect")
    return job


def _accepted_review(row: FleetProfileApplication) -> FleetProfilePreview:
    from .run_switch_operations import _digest

    reviewed = FleetProfilePreview.model_validate_json(
        canonical_message(row.plan), strict=True
    )
    decision = {
        key: value
        for key, value in row.plan.items()
        if key in FleetProfileReviewedDecision.model_fields
    }
    canonical_decision = FleetProfileReviewedDecision.model_validate_json(
        canonical_message(decision), strict=True
    )
    # The accepted producer used canonical_message(model), which omits optional
    # nulls. SQL model_dump retains those nulls; it is not the digested wire.
    # Omit fields introduced since acceptance rather than adding their defaults
    # to the exact old decision's bytes.
    wire = _retained_wire(json.loads(canonical_message(canonical_decision)), decision)
    if _digest(wire) != row.plan_digest:
        raise _UnprovenJournal("accepted profile decision integrity differs from SQL")
    if (reviewed.profile_id, reviewed.profile_digest, reviewed.plan_digest) != (
        row.profile_id,
        row.profile_digest,
        row.plan_digest,
    ):
        raise _UnprovenJournal("accepted profile plan differs from its SQL identity")
    return reviewed


def _retained_wire(wire: object, retained: object) -> object:
    if isinstance(wire, dict) and isinstance(retained, dict):
        return {
            key: _retained_wire(value, retained[key])
            for key, value in wire.items()
            if key in retained
        }
    if isinstance(wire, list) and isinstance(retained, list):
        if len(wire) != len(retained):
            raise _UnprovenJournal("accepted decision changed during wire validation")
        return [
            _retained_wire(value, old)
            for value, old in zip(wire, retained, strict=True)
        ]
    return wire


def _adopted_skip_is_proven(
    session: Session,
    row: FleetProfileApplication,
    progress: FleetProfileApplicationProgress,
    state: FleetProfileSwitchAdapterState,
    reviewed: FleetProfilePreview,
    index: int,
) -> bool:
    """Only current accepted adoption proves an old unissued out-of-scope skip."""
    selection = session.get(FleetProfileSelection, 1)
    if selection is None or selection.application_id == row.id:
        return False
    selected = session.get(FleetProfileApplication, selection.application_id)
    if selected is None or selected.selection_generation != selection.generation:
        return False
    links = [
        link
        for link in _accepted_review(selected).effects.adopted
        if link.application_id == row.id
        and link.plan_digest == row.plan_digest
        and link.workload_intent_ordinal == progress.workload_intent_ordinal
    ]
    if len(links) != 1:
        return False
    link = links[0]
    retained: set[str] = set()
    for assignment_id in link.assignment_ids:
        assignment = next(
            (item for item in state.assignments if item.id == assignment_id), None
        )
        if assignment is None:
            return False
        retained.update(node.node_id for node in assignment.nodes)
    for stop in link.stops:
        effect = next(
            (effect for effect in reviewed.effects.runs if effect == stop.effect), None
        )
        if effect is None or stop.queue_index >= len(state.queue):
            return False
        item = state.queue[stop.queue_index]
        records = (*state.pending_children, *state.children)
        if (
            item.kind != "stop"
            or item.id != effect.run_id
            or stop.request_key
            != profile_switch_child_request_key(
                row.id, stop.queue_index, item.kind, item.id
            )
        ):
            return False
        if not any(
            record.queue_index == stop.queue_index
            and record.operation_id == stop.operation_id
            for record in records
        ):
            return False
        retained.update(effect.node_ids)
    if retained != set(link.node_ids):
        return False
    item = state.queue[index]
    if (
        session.scalar(
            select(Job.id).where(
                Job.request_id
                == profile_switch_child_request_key(row.id, index, item.kind, item.id)
            )
        )
        is not None
    ):
        return False
    if item.kind in {"run", "install"}:
        assignment = next(
            (value for value in state.assignments if value.id == item.id), None
        )
        nodes = (
            set() if assignment is None else {node.node_id for node in assignment.nodes}
        )
    elif item.kind == "stop":
        effect = next(
            (
                value
                for value in reviewed.effects.runs
                if value.run_id == item.id and value.action == "stop"
            ),
            None,
        )
        nodes = (
            set()
            if effect is None
            else set(
                effect.profile_stop_scope.target_node_ids
                if effect.profile_stop_scope is not None
                else effect.node_ids
            )
        )
    else:
        removal = next(
            (
                value
                for value in reviewed.effects.installations
                if value.installation_id == item.id and value.action == "remove"
            ),
            None,
        )
        nodes = set() if removal is None else set(removal.node_ids)
    return bool(nodes) and not nodes <= retained


def _convert(
    session: Session, row: FleetProfileApplication
) -> FleetProfileApplicationProgress:
    raw = copy.deepcopy(row.progress)
    adapter = raw.get("switch_adapter")
    if not isinstance(adapter, dict) or adapter.get("schema_version") != 2:
        raise _UnprovenJournal("retained journal is not schema 2")
    if "active_operation_id" not in adapter or "active_kind" not in adapter:
        raise _UnprovenJournal("retained active identity is incomplete")
    if "pending_children" in adapter or "skipped_indices" in adapter:
        raise _UnprovenJournal("mixed journal encodings are ambiguous")
    active = adapter.pop("active_operation_id")
    kind = adapter.pop("active_kind")
    if (active is None) != (kind is None):
        raise _UnprovenJournal("retained active kind and identity disagree")
    old_children = adapter.pop("children", [])
    if not isinstance(old_children, list):
        raise _UnprovenJournal("retained closed receipts are not a list")
    old_result = adapter.pop("result", None)
    # Validate every unchanged field through the current contract before using it.
    state = FleetProfileSwitchAdapterState.model_validate_json(
        canonical_message(adapter), strict=True
    )
    if state.child_id != row.id:
        raise _UnprovenJournal("retained journal names a different application")
    raw["switch_adapter"] = None
    old_steps = raw.pop("step_results", {})
    progress = FleetProfileApplicationProgress.model_validate_json(
        canonical_message(raw), strict=True
    )
    reviewed = _accepted_review(row)
    if (
        progress.intended_profile is None
        or state.assignments != progress.intended_profile.assignments
    ):
        # The old producer sorts its executable subset; compare exact IDs/bodies,
        # never infer missing assignments from a current saved definition.
        intended = (
            {}
            if progress.intended_profile is None
            else {item.id: item for item in progress.intended_profile.assignments}
        )
        if any(intended.get(item.id) != item for item in state.assignments):
            raise _UnprovenJournal("retained assignments differ from accepted intent")
    used: set[int] = set()
    for raw_child in old_children:
        child = _RetainedChild.model_validate_json(
            canonical_message(raw_child), strict=True
        )
        candidates = [
            index
            for index, item in enumerate(state.queue)
            if index < state.position
            and item.kind == child.kind
            and (job := session.get(Job, child.operation_id)) is not None
            and job.request_id
            == profile_switch_child_request_key(row.id, index, item.kind, item.id)
        ]
        if len(candidates) != 1 or candidates[0] in used:
            raise _UnprovenJournal("closed child has no unique accepted queue binding")
        index = candidates[0]
        job = _job_proof(
            session, row, state, progress, reviewed, index, child.operation_id
        )
        if job.state != child.state:
            raise _UnprovenJournal("closed child state differs from its SQL receipt")
        if child.result is not None:
            if child.result.run_switch_operation_id != job.id:
                raise _UnprovenJournal(
                    "closed child receipt names a different operation"
                )
            current_receipt = RunSwitchOperationResult.model_validate_json(
                canonical_message(job.result), strict=True
            )
            if child.result.run_switch != current_receipt:
                raise _UnprovenJournal(
                    "closed child receipt differs from its SQL result"
                )
        state.children.append(
            FleetProfileSwitchChildState(queue_index=index, **child.model_dump())
        )
        used.add(index)
    if active is not None:
        if (
            not isinstance(active, str)
            or state.position >= len(state.queue)
            or kind != state.queue[state.position].kind
        ):
            raise _UnprovenJournal("active child has no exact current queue index")
        _job_proof(session, row, state, progress, reviewed, state.position, active)
        state.pending_children.append(
            FleetProfileSwitchPendingChild(
                queue_index=state.position,
                operation_id=active,
                kind=state.queue[state.position].kind,
            )
        )
    for index in range(state.position):
        if index in used:
            continue
        item = state.queue[index]
        run = session.get(RecipeRun, item.id) if item.kind == "stop" else None
        effect = next(
            (
                effect
                for effect in reviewed.effects.runs
                if effect.run_id == item.id and effect.action == "stop"
            ),
            None,
        )
        stopped = not (
            run is None
            or run.state != "stopped"
            or effect is None
            or run.installation_id != effect.installation_id
        )
        if not stopped and not _adopted_skip_is_proven(
            session, row, progress, state, reviewed, index
        ):
            raise _UnprovenJournal(
                "unrecorded preceding queue item has no exact completion proof"
            )
        state.skipped_indices.append(index)
    if old_result is not None:
        # A terminal aggregate must contain exactly these proven closed receipts.
        expected = {"children": old_children, "assignment_ids": state.assignment_ids}
        if old_result != expected:
            raise _UnprovenJournal(
                "terminal aggregate differs from its proven queue receipts"
            )
        from .fleet_profile_contract import FleetProfileSwitchAdapterResult

        state.result = FleetProfileSwitchAdapterResult(
            children=state.children, assignment_ids=state.assignment_ids
        )
    progress.switch_adapter = state
    converted = progress.model_dump(mode="json")
    if not isinstance(old_steps, dict):
        raise _UnprovenJournal("retained step results are not an object")
    for receipt in old_steps.values():
        if (
            isinstance(receipt, dict)
            and old_result is not None
            and receipt.get("result") == old_result
        ):
            assert state.result is not None
            receipt["result"] = state.result.model_dump(mode="json")
    converted["step_results"] = old_steps
    return FleetProfileApplicationProgress.model_validate_json(
        canonical_message(converted), strict=True
    )


def try_convert_application(
    session: Session, row: FleetProfileApplication, now: datetime
) -> ProfileAdapterConversionOutcome:
    """Convert one claimed journal atomically, preserving raw evidence on failure."""
    if not needs_conversion(row):
        return ProfileAdapterConversionOutcome(state="current")
    try:
        converted = _convert(session, row)
    except (ValidationError, _UnprovenJournal) as error:
        detail = (
            str(error)
            if isinstance(error, _UnprovenJournal)
            else "retained journal contract is invalid"
        )
        _LOGGER.warning(
            "Profile journal %s conversion deferred (%s): %s",
            row.id,
            type(error).__name__,
            detail,
        )
        row.status_reason = (_DEFERRED + detail + "; retrying automatically")[:512]
        row.updated_at = now
        return conversion_observation(row)
    row.progress = converted.model_dump(mode="json")
    if (row.status_reason or "").startswith(_DEFERRED):
        row.status_reason = None
    return ProfileAdapterConversionOutcome(state="converted")


def convert_due_retained_applications(
    sessions: sessionmaker[Session], now: datetime
) -> int:
    """Advance one bounded conversion page; the worker continues remaining pages."""
    deadline = time.monotonic() + _SCAN_SECONDS
    converted = 0
    consumed = 0
    with sessions.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            adapter = cast(FleetProfileApplication.progress, JSONB)["switch_adapter"]
            marker = or_(
                adapter.op("?")("active_operation_id"), adapter.op("?")("active_kind")
            )
        else:
            marker = or_(
                func.json_type(
                    FleetProfileApplication.progress,
                    "$.switch_adapter.active_operation_id",
                ).is_not(None),
                func.json_type(
                    FleetProfileApplication.progress, "$.switch_adapter.active_kind"
                ).is_not(None),
            )
        rows = session.scalars(
            select(FleetProfileApplication)
            .where(
                marker,
                or_(
                    func.coalesce(FleetProfileApplication.status_reason, "").not_like(
                        _DEFERRED + "%"
                    ),
                    FleetProfileApplication.updated_at <= _aware(now) - _RETRY_INTERVAL,
                ),
            )
            .order_by(FleetProfileApplication.updated_at, FleetProfileApplication.id)
            .limit(_PAGE_ROWS)
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            if time.monotonic() >= deadline or consumed >= _SCAN_BYTES:
                break
            outcome = conversion_observation(row)
            if (
                (row.status_reason or "").startswith(_DEFERRED)
                and outcome.next_attempt_at is not None
                and outcome.next_attempt_at > _aware(now)
            ):
                continue
            consumed += len(canonical_message(row.progress))
            converted += try_convert_application(session, row, now).state == "converted"
    return converted
