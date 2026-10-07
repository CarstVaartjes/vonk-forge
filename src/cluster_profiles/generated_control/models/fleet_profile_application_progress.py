from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.supersede_code import check_supersede_code
from ..models.supersede_code import SupersedeCode
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent
  from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
  from ..models.fleet_profile_child_progress import FleetProfileChildProgress
  from ..models.fleet_profile_effect_progress import FleetProfileEffectProgress
  from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration
  from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
  from ..models.operation_blocker import OperationBlocker





T = TypeVar("T", bound="FleetProfileApplicationProgress")



@_attrs_define
class FleetProfileApplicationProgress:
    """ Typed progress tree persisted with every profile application.

        Attributes:
            admission_attempt (int | Unset):  Default: 0.
            admission_pending (bool | Unset):  Default: False.
            admission_retry_at (datetime.datetime | None | Unset):
            attempt (int | Unset):  Default: 1.
            blockers (list[OperationBlocker] | Unset):
            cancellation (FleetProfileApplicationCancellationIntent | None | Unset):
            child_progress (FleetProfileChildProgress | None | Unset):
            child_source (Literal['switch-adapter'] | None | Unset):
            completed_steps (int | Unset):  Default: 0.
            current_label (None | str | Unset):
            effects (list[FleetProfileEffectProgress] | Unset):
            intended_profile (FleetProfileIntendedConfiguration | None | Unset):
            operation_kind (Literal['fleet-profile.apply'] | None | Unset):
            retry_due_at (datetime.datetime | None | Unset):
            retry_of_application_id (None | str | Unset):
            step_results (FleetProfileApplicationProgressStepResults | Unset):
            storage_wait_since (datetime.datetime | None | Unset):
            supersede_code (None | SupersedeCode | Unset):
            superseded_by (None | str | Unset):
            switch_adapter (FleetProfileSwitchAdapterState | None | Unset):
            total_steps (int | Unset):  Default: 0.
            workload_intent_ordinal (int | None | Unset):
     """

    admission_attempt: int | Unset = 0
    admission_pending: bool | Unset = False
    admission_retry_at: datetime.datetime | None | Unset = UNSET
    attempt: int | Unset = 1
    blockers: list[OperationBlocker] | Unset = UNSET
    cancellation: FleetProfileApplicationCancellationIntent | None | Unset = UNSET
    child_progress: FleetProfileChildProgress | None | Unset = UNSET
    child_source: Literal['switch-adapter'] | None | Unset = UNSET
    completed_steps: int | Unset = 0
    current_label: None | str | Unset = UNSET
    effects: list[FleetProfileEffectProgress] | Unset = UNSET
    intended_profile: FleetProfileIntendedConfiguration | None | Unset = UNSET
    operation_kind: Literal['fleet-profile.apply'] | None | Unset = UNSET
    retry_due_at: datetime.datetime | None | Unset = UNSET
    retry_of_application_id: None | str | Unset = UNSET
    step_results: FleetProfileApplicationProgressStepResults | Unset = UNSET
    storage_wait_since: datetime.datetime | None | Unset = UNSET
    supersede_code: None | SupersedeCode | Unset = UNSET
    superseded_by: None | str | Unset = UNSET
    switch_adapter: FleetProfileSwitchAdapterState | None | Unset = UNSET
    total_steps: int | Unset = 0
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent # noqa: PLC0415
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults # noqa: PLC0415
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress # noqa: PLC0415
        from ..models.fleet_profile_effect_progress import FleetProfileEffectProgress # noqa: PLC0415
        from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration # noqa: PLC0415
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        admission_attempt = self.admission_attempt

        admission_pending = self.admission_pending

        admission_retry_at: None | str | Unset
        if isinstance(self.admission_retry_at, Unset):
            admission_retry_at = UNSET
        elif isinstance(self.admission_retry_at, datetime.datetime):
            admission_retry_at = self.admission_retry_at.isoformat()
        else:
            admission_retry_at = self.admission_retry_at

        attempt = self.attempt

        blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationIntent):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        child_progress: dict[str, Any] | None | Unset
        if isinstance(self.child_progress, Unset):
            child_progress = UNSET
        elif isinstance(self.child_progress, FleetProfileChildProgress):
            child_progress = self.child_progress.to_dict()
        else:
            child_progress = self.child_progress

        child_source: Literal['switch-adapter'] | None | Unset
        if isinstance(self.child_source, Unset):
            child_source = UNSET
        else:
            child_source = self.child_source

        completed_steps = self.completed_steps

        current_label: None | str | Unset
        if isinstance(self.current_label, Unset):
            current_label = UNSET
        else:
            current_label = self.current_label

        effects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.effects, Unset):
            effects = []
            for effects_item_data in self.effects:
                effects_item = effects_item_data.to_dict()
                effects.append(effects_item)



        intended_profile: dict[str, Any] | None | Unset
        if isinstance(self.intended_profile, Unset):
            intended_profile = UNSET
        elif isinstance(self.intended_profile, FleetProfileIntendedConfiguration):
            intended_profile = self.intended_profile.to_dict()
        else:
            intended_profile = self.intended_profile

        operation_kind: Literal['fleet-profile.apply'] | None | Unset
        if isinstance(self.operation_kind, Unset):
            operation_kind = UNSET
        else:
            operation_kind = self.operation_kind

        retry_due_at: None | str | Unset
        if isinstance(self.retry_due_at, Unset):
            retry_due_at = UNSET
        elif isinstance(self.retry_due_at, datetime.datetime):
            retry_due_at = self.retry_due_at.isoformat()
        else:
            retry_due_at = self.retry_due_at

        retry_of_application_id: None | str | Unset
        if isinstance(self.retry_of_application_id, Unset):
            retry_of_application_id = UNSET
        else:
            retry_of_application_id = self.retry_of_application_id

        step_results: dict[str, Any] | Unset = UNSET
        if not isinstance(self.step_results, Unset):
            step_results = self.step_results.to_dict()

        storage_wait_since: None | str | Unset
        if isinstance(self.storage_wait_since, Unset):
            storage_wait_since = UNSET
        elif isinstance(self.storage_wait_since, datetime.datetime):
            storage_wait_since = self.storage_wait_since.isoformat()
        else:
            storage_wait_since = self.storage_wait_since

        supersede_code: None | str | Unset
        if isinstance(self.supersede_code, Unset):
            supersede_code = UNSET
        elif isinstance(self.supersede_code, str):
            supersede_code = self.supersede_code
        else:
            supersede_code = self.supersede_code

        superseded_by: None | str | Unset
        if isinstance(self.superseded_by, Unset):
            superseded_by = UNSET
        else:
            superseded_by = self.superseded_by

        switch_adapter: dict[str, Any] | None | Unset
        if isinstance(self.switch_adapter, Unset):
            switch_adapter = UNSET
        elif isinstance(self.switch_adapter, FleetProfileSwitchAdapterState):
            switch_adapter = self.switch_adapter.to_dict()
        else:
            switch_adapter = self.switch_adapter

        total_steps = self.total_steps

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if admission_attempt is not UNSET:
            field_dict["admission_attempt"] = admission_attempt
        if admission_pending is not UNSET:
            field_dict["admission_pending"] = admission_pending
        if admission_retry_at is not UNSET:
            field_dict["admission_retry_at"] = admission_retry_at
        if attempt is not UNSET:
            field_dict["attempt"] = attempt
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if child_progress is not UNSET:
            field_dict["child_progress"] = child_progress
        if child_source is not UNSET:
            field_dict["child_source"] = child_source
        if completed_steps is not UNSET:
            field_dict["completed_steps"] = completed_steps
        if current_label is not UNSET:
            field_dict["current_label"] = current_label
        if effects is not UNSET:
            field_dict["effects"] = effects
        if intended_profile is not UNSET:
            field_dict["intended_profile"] = intended_profile
        if operation_kind is not UNSET:
            field_dict["operation_kind"] = operation_kind
        if retry_due_at is not UNSET:
            field_dict["retry_due_at"] = retry_due_at
        if retry_of_application_id is not UNSET:
            field_dict["retry_of_application_id"] = retry_of_application_id
        if step_results is not UNSET:
            field_dict["step_results"] = step_results
        if storage_wait_since is not UNSET:
            field_dict["storage_wait_since"] = storage_wait_since
        if supersede_code is not UNSET:
            field_dict["supersede_code"] = supersede_code
        if superseded_by is not UNSET:
            field_dict["superseded_by"] = superseded_by
        if switch_adapter is not UNSET:
            field_dict["switch_adapter"] = switch_adapter
        if total_steps is not UNSET:
            field_dict["total_steps"] = total_steps
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent # noqa: PLC0415
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults # noqa: PLC0415
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress # noqa: PLC0415
        from ..models.fleet_profile_effect_progress import FleetProfileEffectProgress # noqa: PLC0415
        from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration # noqa: PLC0415
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        d = dict(src_dict)
        admission_attempt = d.pop("admission_attempt", UNSET)

        admission_pending = d.pop("admission_pending", UNSET)

        def _parse_admission_retry_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                admission_retry_at_type_0 = datetime.datetime.fromisoformat(data)



                return admission_retry_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        admission_retry_at = _parse_admission_retry_at(d.pop("admission_retry_at", UNSET))


        attempt = d.pop("attempt", UNSET)

        _blockers = d.pop("blockers", UNSET)
        blockers: list[OperationBlocker] | Unset = UNSET
        if _blockers is not UNSET:
            blockers = []
            for blockers_item_data in _blockers:
                blockers_item = OperationBlocker.from_dict(blockers_item_data)



                blockers.append(blockers_item)


        def _parse_cancellation(data: object) -> FleetProfileApplicationCancellationIntent | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = FleetProfileApplicationCancellationIntent.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileApplicationCancellationIntent | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


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


        def _parse_child_source(data: object) -> Literal['switch-adapter'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            child_source_type_0 = cast(Literal['switch-adapter'] , data)
            if child_source_type_0 != 'switch-adapter':
                raise ValueError(f"child_source_type_0 must match const 'switch-adapter', got '{child_source_type_0}'")
            return child_source_type_0
            return cast(Literal['switch-adapter'] | None | Unset, data)

        child_source = _parse_child_source(d.pop("child_source", UNSET))


        completed_steps = d.pop("completed_steps", UNSET)

        def _parse_current_label(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_label = _parse_current_label(d.pop("current_label", UNSET))


        _effects = d.pop("effects", UNSET)
        effects: list[FleetProfileEffectProgress] | Unset = UNSET
        if _effects is not UNSET:
            effects = []
            for effects_item_data in _effects:
                effects_item = FleetProfileEffectProgress.from_dict(effects_item_data)



                effects.append(effects_item)


        def _parse_intended_profile(data: object) -> FleetProfileIntendedConfiguration | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intended_profile_type_0 = FleetProfileIntendedConfiguration.from_dict(data)



                return intended_profile_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileIntendedConfiguration | None | Unset, data)

        intended_profile = _parse_intended_profile(d.pop("intended_profile", UNSET))


        def _parse_operation_kind(data: object) -> Literal['fleet-profile.apply'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            operation_kind_type_0 = cast(Literal['fleet-profile.apply'] , data)
            if operation_kind_type_0 != 'fleet-profile.apply':
                raise ValueError(f"operation_kind_type_0 must match const 'fleet-profile.apply', got '{operation_kind_type_0}'")
            return operation_kind_type_0
            return cast(Literal['fleet-profile.apply'] | None | Unset, data)

        operation_kind = _parse_operation_kind(d.pop("operation_kind", UNSET))


        def _parse_retry_due_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                retry_due_at_type_0 = datetime.datetime.fromisoformat(data)



                return retry_due_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        retry_due_at = _parse_retry_due_at(d.pop("retry_due_at", UNSET))


        def _parse_retry_of_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_of_application_id = _parse_retry_of_application_id(d.pop("retry_of_application_id", UNSET))


        _step_results = d.pop("step_results", UNSET)
        step_results: FleetProfileApplicationProgressStepResults | Unset
        if isinstance(_step_results,  Unset):
            step_results = UNSET
        else:
            step_results = FleetProfileApplicationProgressStepResults.from_dict(_step_results)




        def _parse_storage_wait_since(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                storage_wait_since_type_0 = datetime.datetime.fromisoformat(data)



                return storage_wait_since_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        storage_wait_since = _parse_storage_wait_since(d.pop("storage_wait_since", UNSET))


        def _parse_supersede_code(data: object) -> None | SupersedeCode | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                supersede_code_type_0 = check_supersede_code(data)



                return supersede_code_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SupersedeCode | Unset, data)

        supersede_code = _parse_supersede_code(d.pop("supersede_code", UNSET))


        def _parse_superseded_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        superseded_by = _parse_superseded_by(d.pop("superseded_by", UNSET))


        def _parse_switch_adapter(data: object) -> FleetProfileSwitchAdapterState | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                switch_adapter_type_0 = FleetProfileSwitchAdapterState.from_dict(data)



                return switch_adapter_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileSwitchAdapterState | None | Unset, data)

        switch_adapter = _parse_switch_adapter(d.pop("switch_adapter", UNSET))


        total_steps = d.pop("total_steps", UNSET)

        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        fleet_profile_application_progress = cls(
            admission_attempt=admission_attempt,
            admission_pending=admission_pending,
            admission_retry_at=admission_retry_at,
            attempt=attempt,
            blockers=blockers,
            cancellation=cancellation,
            child_progress=child_progress,
            child_source=child_source,
            completed_steps=completed_steps,
            current_label=current_label,
            effects=effects,
            intended_profile=intended_profile,
            operation_kind=operation_kind,
            retry_due_at=retry_due_at,
            retry_of_application_id=retry_of_application_id,
            step_results=step_results,
            storage_wait_since=storage_wait_since,
            supersede_code=supersede_code,
            superseded_by=superseded_by,
            switch_adapter=switch_adapter,
            total_steps=total_steps,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return fleet_profile_application_progress
