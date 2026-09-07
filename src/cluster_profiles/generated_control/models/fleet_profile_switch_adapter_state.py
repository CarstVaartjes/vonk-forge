from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_adapter_state_active_kind_type_0 import check_fleet_profile_switch_adapter_state_active_kind_type_0
from ..models.fleet_profile_switch_adapter_state_active_kind_type_0 import FleetProfileSwitchAdapterStateActiveKindType0
from ..models.fleet_profile_switch_adapter_state_state import check_fleet_profile_switch_adapter_state_state
from ..models.fleet_profile_switch_adapter_state_state import FleetProfileSwitchAdapterStateState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
  from ..models.fleet_profile_child_progress import FleetProfileChildProgress
  from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
  from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem
  from ..models.fleet_profile_assignment import FleetProfileAssignment





T = TypeVar("T", bound="FleetProfileSwitchAdapterState")



@_attrs_define
class FleetProfileSwitchAdapterState:
    """ Persisted state used to resume a profile switch after restart.

        Attributes:
            actor (str):
            assignment_ids (list[str]):
            assignments (list['FleetProfileAssignment']):
            child_id (str):
            queue (list['FleetProfileSwitchQueueItem']):
            request_id (str):
            scope_node_ids (list[str]):
            active_kind (Union[FleetProfileSwitchAdapterStateActiveKindType0, None, Unset]):
            active_operation_id (Union[None, Unset, str]):
            child_progress (Union['FleetProfileChildProgress', None, Unset]):
            children (Union[Unset, list['FleetProfileSwitchChildState']]):
            position (Union[Unset, int]):  Default: 0.
            result (Union['FleetProfileSwitchAdapterResult', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            state (Union[Unset, FleetProfileSwitchAdapterStateState]):  Default: 'queued'.
            status_reason (Union[None, Unset, str]):
     """

    actor: str
    assignment_ids: list[str]
    assignments: list['FleetProfileAssignment']
    child_id: str
    queue: list['FleetProfileSwitchQueueItem']
    request_id: str
    scope_node_ids: list[str]
    active_kind: Union[FleetProfileSwitchAdapterStateActiveKindType0, None, Unset] = UNSET
    active_operation_id: Union[None, Unset, str] = UNSET
    child_progress: Union['FleetProfileChildProgress', None, Unset] = UNSET
    children: Union[Unset, list['FleetProfileSwitchChildState']] = UNSET
    position: Union[Unset, int] = 0
    result: Union['FleetProfileSwitchAdapterResult', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    state: Union[Unset, FleetProfileSwitchAdapterStateState] = 'queued'
    status_reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
        from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem
        from ..models.fleet_profile_assignment import FleetProfileAssignment
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



        active_kind: Union[None, Unset, str]
        if isinstance(self.active_kind, Unset):
            active_kind = UNSET
        elif isinstance(self.active_kind, str):
            active_kind = self.active_kind
        else:
            active_kind = self.active_kind

        active_operation_id: Union[None, Unset, str]
        if isinstance(self.active_operation_id, Unset):
            active_operation_id = UNSET
        else:
            active_operation_id = self.active_operation_id

        child_progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.child_progress, Unset):
            child_progress = UNSET
        elif isinstance(self.child_progress, FleetProfileChildProgress):
            child_progress = self.child_progress.to_dict()
        else:
            child_progress = self.child_progress

        children: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.children, Unset):
            children = []
            for children_item_data in self.children:
                children_item = children_item_data.to_dict()
                children.append(children_item)



        position = self.position

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileSwitchAdapterResult):
            result = self.result.to_dict()
        else:
            result = self.result

        schema_version = self.schema_version

        state: Union[Unset, str] = UNSET
        if not isinstance(self.state, Unset):
            state = self.state


        status_reason: Union[None, Unset, str]
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason


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
        if active_kind is not UNSET:
            field_dict["active_kind"] = active_kind
        if active_operation_id is not UNSET:
            field_dict["active_operation_id"] = active_operation_id
        if child_progress is not UNSET:
            field_dict["child_progress"] = child_progress
        if children is not UNSET:
            field_dict["children"] = children
        if position is not UNSET:
            field_dict["position"] = position
        if result is not UNSET:
            field_dict["result"] = result
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if state is not UNSET:
            field_dict["state"] = state
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
        from ..models.fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem
        from ..models.fleet_profile_assignment import FleetProfileAssignment
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


        def _parse_active_kind(data: object) -> Union[FleetProfileSwitchAdapterStateActiveKindType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                active_kind_type_0 = check_fleet_profile_switch_adapter_state_active_kind_type_0(data)



                return active_kind_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileSwitchAdapterStateActiveKindType0, None, Unset], data)

        active_kind = _parse_active_kind(d.pop("active_kind", UNSET))


        def _parse_active_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        active_operation_id = _parse_active_operation_id(d.pop("active_operation_id", UNSET))


        def _parse_child_progress(data: object) -> Union['FleetProfileChildProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                child_progress_type_0 = FleetProfileChildProgress.from_dict(data)



                return child_progress_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileChildProgress', None, Unset], data)

        child_progress = _parse_child_progress(d.pop("child_progress", UNSET))


        children = []
        _children = d.pop("children", UNSET)
        for children_item_data in (_children or []):
            children_item = FleetProfileSwitchChildState.from_dict(children_item_data)



            children.append(children_item)


        position = d.pop("position", UNSET)

        def _parse_result(data: object) -> Union['FleetProfileSwitchAdapterResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchAdapterResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileSwitchAdapterResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        _state = d.pop("state", UNSET)
        state: Union[Unset, FleetProfileSwitchAdapterStateState]
        if isinstance(_state,  Unset):
            state = UNSET
        else:
            state = check_fleet_profile_switch_adapter_state_state(_state)




        def _parse_status_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        fleet_profile_switch_adapter_state = cls(
            actor=actor,
            assignment_ids=assignment_ids,
            assignments=assignments,
            child_id=child_id,
            queue=queue,
            request_id=request_id,
            scope_node_ids=scope_node_ids,
            active_kind=active_kind,
            active_operation_id=active_operation_id,
            child_progress=child_progress,
            children=children,
            position=position,
            result=result,
            schema_version=schema_version,
            state=state,
            status_reason=status_reason,
        )

        return fleet_profile_switch_adapter_state
