from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_operation_action import check_run_switch_operation_action
from ..models.run_switch_operation_action import RunSwitchOperationAction
from ..models.run_switch_operation_completed_phases_item import check_run_switch_operation_completed_phases_item
from ..models.run_switch_operation_completed_phases_item import RunSwitchOperationCompletedPhasesItem
from ..models.run_switch_operation_current_phase_type_0 import check_run_switch_operation_current_phase_type_0
from ..models.run_switch_operation_current_phase_type_0 import RunSwitchOperationCurrentPhaseType0
from ..models.run_switch_operation_kind import check_run_switch_operation_kind
from ..models.run_switch_operation_kind import RunSwitchOperationKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_operation_result_type_0 import RunSwitchOperationResultType0
  from ..models.run_switch_progress import RunSwitchProgress





T = TypeVar("T", bound="RunSwitchOperation")



@_attrs_define
class RunSwitchOperation:
    """
        Attributes:
            action (RunSwitchOperationAction):
            completed_phases (list[RunSwitchOperationCompletedPhasesItem]):
            kind (RunSwitchOperationKind):
            node_ids (list[str]):
            operation_id (str):
            plan_digest (str):
            progress (RunSwitchProgress):
            request_key (str):
            state (str):
            current_phase (Union[None, RunSwitchOperationCurrentPhaseType0, Unset]):
            result (Union['RunSwitchOperationResultType0', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            status_reason (Union[None, Unset, str]):
     """

    action: RunSwitchOperationAction
    completed_phases: list[RunSwitchOperationCompletedPhasesItem]
    kind: RunSwitchOperationKind
    node_ids: list[str]
    operation_id: str
    plan_digest: str
    progress: 'RunSwitchProgress'
    request_key: str
    state: str
    current_phase: Union[None, RunSwitchOperationCurrentPhaseType0, Unset] = UNSET
    result: Union['RunSwitchOperationResultType0', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    status_reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_operation_result_type_0 import RunSwitchOperationResultType0
        from ..models.run_switch_progress import RunSwitchProgress
        action: str = self.action

        completed_phases = []
        for completed_phases_item_data in self.completed_phases:
            completed_phases_item: str = completed_phases_item_data
            completed_phases.append(completed_phases_item)



        kind: str = self.kind

        node_ids = self.node_ids



        operation_id = self.operation_id

        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        request_key = self.request_key

        state = self.state

        current_phase: Union[None, Unset, str]
        if isinstance(self.current_phase, Unset):
            current_phase = UNSET
        elif isinstance(self.current_phase, str):
            current_phase = self.current_phase
        else:
            current_phase = self.current_phase

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, RunSwitchOperationResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        schema_version = self.schema_version

        status_reason: Union[None, Unset, str]
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "completed_phases": completed_phases,
            "kind": kind,
            "node_ids": node_ids,
            "operation_id": operation_id,
            "plan_digest": plan_digest,
            "progress": progress,
            "request_key": request_key,
            "state": state,
        })
        if current_phase is not UNSET:
            field_dict["current_phase"] = current_phase
        if result is not UNSET:
            field_dict["result"] = result
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_operation_result_type_0 import RunSwitchOperationResultType0
        from ..models.run_switch_progress import RunSwitchProgress
        d = dict(src_dict)
        action = check_run_switch_operation_action(d.pop("action"))




        completed_phases = []
        _completed_phases = d.pop("completed_phases")
        for completed_phases_item_data in (_completed_phases):
            completed_phases_item = check_run_switch_operation_completed_phases_item(completed_phases_item_data)



            completed_phases.append(completed_phases_item)


        kind = check_run_switch_operation_kind(d.pop("kind"))




        node_ids = cast(list[str], d.pop("node_ids"))


        operation_id = d.pop("operation_id")

        plan_digest = d.pop("plan_digest")

        progress = RunSwitchProgress.from_dict(d.pop("progress"))




        request_key = d.pop("request_key")

        state = d.pop("state")

        def _parse_current_phase(data: object) -> Union[None, RunSwitchOperationCurrentPhaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                current_phase_type_0 = check_run_switch_operation_current_phase_type_0(data)



                return current_phase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchOperationCurrentPhaseType0, Unset], data)

        current_phase = _parse_current_phase(d.pop("current_phase", UNSET))


        def _parse_result(data: object) -> Union['RunSwitchOperationResultType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = RunSwitchOperationResultType0.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['RunSwitchOperationResultType0', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_status_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        run_switch_operation = cls(
            action=action,
            completed_phases=completed_phases,
            kind=kind,
            node_ids=node_ids,
            operation_id=operation_id,
            plan_digest=plan_digest,
            progress=progress,
            request_key=request_key,
            state=state,
            current_phase=current_phase,
            result=result,
            schema_version=schema_version,
            status_reason=status_reason,
        )

        return run_switch_operation
