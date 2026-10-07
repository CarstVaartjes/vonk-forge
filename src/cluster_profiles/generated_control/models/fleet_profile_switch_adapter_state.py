from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_adapter_state_state import check_fleet_profile_switch_adapter_state_state
from ..models.fleet_profile_switch_adapter_state_state import FleetProfileSwitchAdapterStateState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment import FleetProfileAssignment
  from ..models.fleet_profile_assignment_failure import FleetProfileAssignmentFailure
  from ..models.fleet_profile_child_progress import FleetProfileChildProgress
  from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
  from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
  from ..models.fleet_profile_switch_pending_child import FleetProfileSwitchPendingChild
  from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem





T = TypeVar("T", bound="FleetProfileSwitchAdapterState")



@_attrs_define
class FleetProfileSwitchAdapterState:
    """ Persisted state used to resume a profile switch after restart.

        Attributes:
            actor (str):
            assignment_ids (list[str]):
            assignments (list[FleetProfileAssignment]):
            child_id (str):
            queue (list[FleetProfileSwitchQueueItem]):
            request_id (str):
            scope_node_ids (list[str]):
            assignment_failures (list[FleetProfileAssignmentFailure] | Unset):
            child_progress (FleetProfileChildProgress | None | Unset):
            children (list[FleetProfileSwitchChildState] | Unset):
            observation_deadline_at (datetime.datetime | None | Unset):
            observation_due_at (datetime.datetime | None | Unset):
            pending_children (list[FleetProfileSwitchPendingChild] | Unset):
            pending_operation_ids (list[str] | Unset):
            position (int | Unset):  Default: 0.
            result (FleetProfileSwitchAdapterResult | None | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
            skipped_indices (list[int] | Unset):
            state (FleetProfileSwitchAdapterStateState | Unset):  Default: 'queued'.
            status_reason (None | str | Unset):
            stop_reissue_attempt (int | Unset):  Default: 0.
     """

    actor: str
    assignment_ids: list[str]
    assignments: list[FleetProfileAssignment]
    child_id: str
    queue: list[FleetProfileSwitchQueueItem]
    request_id: str
    scope_node_ids: list[str]
    assignment_failures: list[FleetProfileAssignmentFailure] | Unset = UNSET
    child_progress: FleetProfileChildProgress | None | Unset = UNSET
    children: list[FleetProfileSwitchChildState] | Unset = UNSET
    observation_deadline_at: datetime.datetime | None | Unset = UNSET
    observation_due_at: datetime.datetime | None | Unset = UNSET
    pending_children: list[FleetProfileSwitchPendingChild] | Unset = UNSET
    pending_operation_ids: list[str] | Unset = UNSET
    position: int | Unset = 0
    result: FleetProfileSwitchAdapterResult | None | Unset = UNSET
    schema_version: Literal[2] | Unset = 2
    skipped_indices: list[int] | Unset = UNSET
    state: FleetProfileSwitchAdapterStateState | Unset = 'queued'
    status_reason: None | str | Unset = UNSET
    stop_reissue_attempt: int | Unset = 0





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment import FleetProfileAssignment # noqa: PLC0415
        from ..models.fleet_profile_assignment_failure import FleetProfileAssignmentFailure # noqa: PLC0415
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress # noqa: PLC0415
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult # noqa: PLC0415
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState # noqa: PLC0415
        from ..models.fleet_profile_switch_pending_child import FleetProfileSwitchPendingChild # noqa: PLC0415
        from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem # noqa: PLC0415
        actor = self.actor

        assignment_ids = self.assignment_ids



        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        child_id = self.child_id

        queue = []
        for queue_item_data in self.queue:
            queue_item = queue_item_data.to_dict()
            queue.append(queue_item)



        request_id = self.request_id

        scope_node_ids = self.scope_node_ids



        assignment_failures: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.assignment_failures, Unset):
            assignment_failures = []
            for assignment_failures_item_data in self.assignment_failures:
                assignment_failures_item = assignment_failures_item_data.to_dict()
                assignment_failures.append(assignment_failures_item)



        child_progress: dict[str, Any] | None | Unset
        if isinstance(self.child_progress, Unset):
            child_progress = UNSET
        elif isinstance(self.child_progress, FleetProfileChildProgress):
            child_progress = self.child_progress.to_dict()
        else:
            child_progress = self.child_progress

        children: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.children, Unset):
            children = []
            for children_item_data in self.children:
                children_item = children_item_data.to_dict()
                children.append(children_item)



        observation_deadline_at: None | str | Unset
        if isinstance(self.observation_deadline_at, Unset):
            observation_deadline_at = UNSET
        elif isinstance(self.observation_deadline_at, datetime.datetime):
            observation_deadline_at = self.observation_deadline_at.isoformat()
        else:
            observation_deadline_at = self.observation_deadline_at

        observation_due_at: None | str | Unset
        if isinstance(self.observation_due_at, Unset):
            observation_due_at = UNSET
        elif isinstance(self.observation_due_at, datetime.datetime):
            observation_due_at = self.observation_due_at.isoformat()
        else:
            observation_due_at = self.observation_due_at

        pending_children: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.pending_children, Unset):
            pending_children = []
            for pending_children_item_data in self.pending_children:
                pending_children_item = pending_children_item_data.to_dict()
                pending_children.append(pending_children_item)



        pending_operation_ids: list[str] | Unset = UNSET
        if not isinstance(self.pending_operation_ids, Unset):
            pending_operation_ids = self.pending_operation_ids



        position = self.position

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileSwitchAdapterResult):
            result = self.result.to_dict()
        else:
            result = self.result

        schema_version = self.schema_version

        skipped_indices: list[int] | Unset = UNSET
        if not isinstance(self.skipped_indices, Unset):
            skipped_indices = self.skipped_indices



        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state


        status_reason: None | str | Unset
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason

        stop_reissue_attempt = self.stop_reissue_attempt


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "assignment_ids": assignment_ids,
            "assignments": assignments,
            "child_id": child_id,
            "queue": queue,
            "request_id": request_id,
            "scope_node_ids": scope_node_ids,
        })
        if assignment_failures is not UNSET:
            field_dict["assignment_failures"] = assignment_failures
        if child_progress is not UNSET:
            field_dict["child_progress"] = child_progress
        if children is not UNSET:
            field_dict["children"] = children
        if observation_deadline_at is not UNSET:
            field_dict["observation_deadline_at"] = observation_deadline_at
        if observation_due_at is not UNSET:
            field_dict["observation_due_at"] = observation_due_at
        if pending_children is not UNSET:
            field_dict["pending_children"] = pending_children
        if pending_operation_ids is not UNSET:
            field_dict["pending_operation_ids"] = pending_operation_ids
        if position is not UNSET:
            field_dict["position"] = position
        if result is not UNSET:
            field_dict["result"] = result
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if skipped_indices is not UNSET:
            field_dict["skipped_indices"] = skipped_indices
        if state is not UNSET:
            field_dict["state"] = state
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason
        if stop_reissue_attempt is not UNSET:
            field_dict["stop_reissue_attempt"] = stop_reissue_attempt

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_assignment import FleetProfileAssignment # noqa: PLC0415
        from ..models.fleet_profile_assignment_failure import FleetProfileAssignmentFailure # noqa: PLC0415
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress # noqa: PLC0415
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult # noqa: PLC0415
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState # noqa: PLC0415
        from ..models.fleet_profile_switch_pending_child import FleetProfileSwitchPendingChild # noqa: PLC0415
        from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem # noqa: PLC0415
        d = dict(src_dict)
        actor = d.pop("actor")

        assignment_ids = cast(list[str], d.pop("assignment_ids"))


        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignment.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        child_id = d.pop("child_id")

        queue = []
        _queue = d.pop("queue")
        for queue_item_data in (_queue):
            queue_item = FleetProfileSwitchQueueItem.from_dict(queue_item_data)



            queue.append(queue_item)


        request_id = d.pop("request_id")

        scope_node_ids = cast(list[str], d.pop("scope_node_ids"))


        _assignment_failures = d.pop("assignment_failures", UNSET)
        assignment_failures: list[FleetProfileAssignmentFailure] | Unset = UNSET
        if _assignment_failures is not UNSET:
            assignment_failures = []
            for assignment_failures_item_data in _assignment_failures:
                assignment_failures_item = FleetProfileAssignmentFailure.from_dict(assignment_failures_item_data)



                assignment_failures.append(assignment_failures_item)


        def _parse_child_progress(data: object) -> FleetProfileChildProgress | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                child_progress_type_0 = FleetProfileChildProgress.from_dict(data)



                return child_progress_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileChildProgress | None | Unset, data)

        child_progress = _parse_child_progress(d.pop("child_progress", UNSET))


        _children = d.pop("children", UNSET)
        children: list[FleetProfileSwitchChildState] | Unset = UNSET
        if _children is not UNSET:
            children = []
            for children_item_data in _children:
                children_item = FleetProfileSwitchChildState.from_dict(children_item_data)



                children.append(children_item)


        def _parse_observation_deadline_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observation_deadline_at_type_0 = datetime.datetime.fromisoformat(data)



                return observation_deadline_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        observation_deadline_at = _parse_observation_deadline_at(d.pop("observation_deadline_at", UNSET))


        def _parse_observation_due_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observation_due_at_type_0 = datetime.datetime.fromisoformat(data)



                return observation_due_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        observation_due_at = _parse_observation_due_at(d.pop("observation_due_at", UNSET))


        _pending_children = d.pop("pending_children", UNSET)
        pending_children: list[FleetProfileSwitchPendingChild] | Unset = UNSET
        if _pending_children is not UNSET:
            pending_children = []
            for pending_children_item_data in _pending_children:
                pending_children_item = FleetProfileSwitchPendingChild.from_dict(pending_children_item_data)



                pending_children.append(pending_children_item)


        pending_operation_ids = cast(list[str], d.pop("pending_operation_ids", UNSET))


        position = d.pop("position", UNSET)

        def _parse_result(data: object) -> FleetProfileSwitchAdapterResult | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchAdapterResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileSwitchAdapterResult | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        skipped_indices = cast(list[int], d.pop("skipped_indices", UNSET))


        _state = d.pop("state", UNSET)
        state: FleetProfileSwitchAdapterStateState | Unset
        if isinstance(_state,  Unset):
            state = UNSET
        else:
            state = check_fleet_profile_switch_adapter_state_state(_state)




        def _parse_status_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        stop_reissue_attempt = d.pop("stop_reissue_attempt", UNSET)

        fleet_profile_switch_adapter_state = cls(
            actor=actor,
            assignment_ids=assignment_ids,
            assignments=assignments,
            child_id=child_id,
            queue=queue,
            request_id=request_id,
            scope_node_ids=scope_node_ids,
            assignment_failures=assignment_failures,
            child_progress=child_progress,
            children=children,
            observation_deadline_at=observation_deadline_at,
            observation_due_at=observation_due_at,
            pending_children=pending_children,
            pending_operation_ids=pending_operation_ids,
            position=position,
            result=result,
            schema_version=schema_version,
            skipped_indices=skipped_indices,
            state=state,
            status_reason=status_reason,
            stop_reissue_attempt=stop_reissue_attempt,
        )

        return fleet_profile_switch_adapter_state
