from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_effect_progress_kind import check_fleet_profile_effect_progress_kind
from ..models.fleet_profile_effect_progress_kind import FleetProfileEffectProgressKind
from ..models.fleet_profile_effect_progress_state import check_fleet_profile_effect_progress_state
from ..models.fleet_profile_effect_progress_state import FleetProfileEffectProgressState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_run_effect import FleetProfileRunEffect
  from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult
  from ..models.run_switch_progress import RunSwitchProgress





T = TypeVar("T", bound="FleetProfileEffectProgress")



@_attrs_define
class FleetProfileEffectProgress:
    """ Observational receipt for one immutable accepted queue effect.

        Attributes:
            application_id (str):
            effect_id (str):
            kind (FleetProfileEffectProgressKind):
            node_ids (list[str]):
            plan_digest (str):
            queue_index (int):
            request_key (str):
            state (FleetProfileEffectProgressState):
            target_id (str):
            workload_intent_ordinal (int):
            operation_id (None | str | Unset):
            original_operation_id (None | str | Unset):
            progress (None | RunSwitchProgress | Unset):
            result (FleetProfileSwitchChildResult | None | Unset):
            stop_effect (FleetProfileRunEffect | None | Unset):
     """

    application_id: str
    effect_id: str
    kind: FleetProfileEffectProgressKind
    node_ids: list[str]
    plan_digest: str
    queue_index: int
    request_key: str
    state: FleetProfileEffectProgressState
    target_id: str
    workload_intent_ordinal: int
    operation_id: None | str | Unset = UNSET
    original_operation_id: None | str | Unset = UNSET
    progress: None | RunSwitchProgress | Unset = UNSET
    result: FleetProfileSwitchChildResult | None | Unset = UNSET
    stop_effect: FleetProfileRunEffect | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect # noqa: PLC0415
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult # noqa: PLC0415
        from ..models.run_switch_progress import RunSwitchProgress # noqa: PLC0415
        application_id = self.application_id

        effect_id = self.effect_id

        kind: str = self.kind

        node_ids = self.node_ids



        plan_digest = self.plan_digest

        queue_index = self.queue_index

        request_key = self.request_key

        state: str = self.state

        target_id = self.target_id

        workload_intent_ordinal = self.workload_intent_ordinal

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        original_operation_id: None | str | Unset
        if isinstance(self.original_operation_id, Unset):
            original_operation_id = UNSET
        else:
            original_operation_id = self.original_operation_id

        progress: dict[str, Any] | None | Unset
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, RunSwitchProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileSwitchChildResult):
            result = self.result.to_dict()
        else:
            result = self.result

        stop_effect: dict[str, Any] | None | Unset
        if isinstance(self.stop_effect, Unset):
            stop_effect = UNSET
        elif isinstance(self.stop_effect, FleetProfileRunEffect):
            stop_effect = self.stop_effect.to_dict()
        else:
            stop_effect = self.stop_effect


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "application_id": application_id,
            "effect_id": effect_id,
            "kind": kind,
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "queue_index": queue_index,
            "request_key": request_key,
            "state": state,
            "target_id": target_id,
            "workload_intent_ordinal": workload_intent_ordinal,
        })
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if original_operation_id is not UNSET:
            field_dict["original_operation_id"] = original_operation_id
        if progress is not UNSET:
            field_dict["progress"] = progress
        if result is not UNSET:
            field_dict["result"] = result
        if stop_effect is not UNSET:
            field_dict["stop_effect"] = stop_effect

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect # noqa: PLC0415
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult # noqa: PLC0415
        from ..models.run_switch_progress import RunSwitchProgress # noqa: PLC0415
        d = dict(src_dict)
        application_id = d.pop("application_id")

        effect_id = d.pop("effect_id")

        kind = check_fleet_profile_effect_progress_kind(d.pop("kind"))




        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        queue_index = d.pop("queue_index")

        request_key = d.pop("request_key")

        state = check_fleet_profile_effect_progress_state(d.pop("state"))




        target_id = d.pop("target_id")

        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        def _parse_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_original_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        original_operation_id = _parse_original_operation_id(d.pop("original_operation_id", UNSET))


        def _parse_progress(data: object) -> None | RunSwitchProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = RunSwitchProgress.from_dict(data)



                return progress_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchProgress | Unset, data)

        progress = _parse_progress(d.pop("progress", UNSET))


        def _parse_result(data: object) -> FleetProfileSwitchChildResult | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchChildResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileSwitchChildResult | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))


        def _parse_stop_effect(data: object) -> FleetProfileRunEffect | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                stop_effect_type_0 = FleetProfileRunEffect.from_dict(data)



                return stop_effect_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileRunEffect | None | Unset, data)

        stop_effect = _parse_stop_effect(d.pop("stop_effect", UNSET))


        fleet_profile_effect_progress = cls(
            application_id=application_id,
            effect_id=effect_id,
            kind=kind,
            node_ids=node_ids,
            plan_digest=plan_digest,
            queue_index=queue_index,
            request_key=request_key,
            state=state,
            target_id=target_id,
            workload_intent_ordinal=workload_intent_ordinal,
            operation_id=operation_id,
            original_operation_id=original_operation_id,
            progress=progress,
            result=result,
            stop_effect=stop_effect,
        )

        return fleet_profile_effect_progress
