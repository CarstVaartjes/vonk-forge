from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_operation_action import check_run_switch_operation_action
from ..models.run_switch_operation_action import RunSwitchOperationAction
from ..models.run_switch_operation_cleanup_mode_type_0 import check_run_switch_operation_cleanup_mode_type_0
from ..models.run_switch_operation_cleanup_mode_type_0 import RunSwitchOperationCleanupModeType0
from ..models.run_switch_operation_completed_phases_item import check_run_switch_operation_completed_phases_item
from ..models.run_switch_operation_completed_phases_item import RunSwitchOperationCompletedPhasesItem
from ..models.run_switch_operation_current_phase_type_0 import check_run_switch_operation_current_phase_type_0
from ..models.run_switch_operation_current_phase_type_0 import RunSwitchOperationCurrentPhaseType0
from ..models.run_switch_operation_kind import check_run_switch_operation_kind
from ..models.run_switch_operation_kind import RunSwitchOperationKind
from ..models.run_switch_operation_state import check_run_switch_operation_state
from ..models.run_switch_operation_state import RunSwitchOperationState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_operation_result import RunSwitchOperationResult
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
            state (RunSwitchOperationState):
            cleanup_mode (None | RunSwitchOperationCleanupModeType0 | Unset):
            current_phase (None | RunSwitchOperationCurrentPhaseType0 | Unset):
            installation_id (None | str | Unset):
            result (None | RunSwitchOperationResult | Unset):
            status_reason (None | str | Unset):
     """

    action: RunSwitchOperationAction
    completed_phases: list[RunSwitchOperationCompletedPhasesItem]
    kind: RunSwitchOperationKind
    node_ids: list[str]
    operation_id: str
    plan_digest: str
    progress: RunSwitchProgress
    request_key: str
    state: RunSwitchOperationState
    cleanup_mode: None | RunSwitchOperationCleanupModeType0 | Unset = UNSET
    current_phase: None | RunSwitchOperationCurrentPhaseType0 | Unset = UNSET
    installation_id: None | str | Unset = UNSET
    result: None | RunSwitchOperationResult | Unset = UNSET
    status_reason: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_operation_result import RunSwitchOperationResult # noqa: PLC0415
        from ..models.run_switch_progress import RunSwitchProgress # noqa: PLC0415
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

        state: str = self.state

        cleanup_mode: None | str | Unset
        if isinstance(self.cleanup_mode, Unset):
            cleanup_mode = UNSET
        elif isinstance(self.cleanup_mode, str):
            cleanup_mode = self.cleanup_mode
        else:
            cleanup_mode = self.cleanup_mode

        current_phase: None | str | Unset
        if isinstance(self.current_phase, Unset):
            current_phase = UNSET
        elif isinstance(self.current_phase, str):
            current_phase = self.current_phase
        else:
            current_phase = self.current_phase

        installation_id: None | str | Unset
        if isinstance(self.installation_id, Unset):
            installation_id = UNSET
        else:
            installation_id = self.installation_id

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, RunSwitchOperationResult):
            result = self.result.to_dict()
        else:
            result = self.result

        status_reason: None | str | Unset
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
        if cleanup_mode is not UNSET:
            field_dict["cleanup_mode"] = cleanup_mode
        if current_phase is not UNSET:
            field_dict["current_phase"] = current_phase
        if installation_id is not UNSET:
            field_dict["installation_id"] = installation_id
        if result is not UNSET:
            field_dict["result"] = result
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_operation_result import RunSwitchOperationResult # noqa: PLC0415
        from ..models.run_switch_progress import RunSwitchProgress # noqa: PLC0415
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

        state = check_run_switch_operation_state(d.pop("state"))




        def _parse_cleanup_mode(data: object) -> None | RunSwitchOperationCleanupModeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                cleanup_mode_type_0 = check_run_switch_operation_cleanup_mode_type_0(data)



                return cleanup_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchOperationCleanupModeType0 | Unset, data)

        cleanup_mode = _parse_cleanup_mode(d.pop("cleanup_mode", UNSET))


        def _parse_current_phase(data: object) -> None | RunSwitchOperationCurrentPhaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                current_phase_type_0 = check_run_switch_operation_current_phase_type_0(data)



                return current_phase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchOperationCurrentPhaseType0 | Unset, data)

        current_phase = _parse_current_phase(d.pop("current_phase", UNSET))


        def _parse_installation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        installation_id = _parse_installation_id(d.pop("installation_id", UNSET))


        def _parse_result(data: object) -> None | RunSwitchOperationResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = RunSwitchOperationResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchOperationResult | Unset, data)

        result = _parse_result(d.pop("result", UNSET))


        def _parse_status_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
            cleanup_mode=cleanup_mode,
            current_phase=current_phase,
            installation_id=installation_id,
            result=result,
            status_reason=status_reason,
        )

        return run_switch_operation
