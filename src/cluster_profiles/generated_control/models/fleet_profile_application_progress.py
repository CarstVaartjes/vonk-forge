from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent
  from ..models.fleet_profile_child_progress import FleetProfileChildProgress
  from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
  from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration
  from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState





T = TypeVar("T", bound="FleetProfileApplicationProgress")



@_attrs_define
class FleetProfileApplicationProgress:
    """ Typed progress tree persisted with every profile application.

        Attributes:
            admission_attempt (Union[Unset, int]):  Default: 0.
            admission_pending (Union[Unset, bool]):  Default: False.
            admission_retry_at (Union[None, Unset, datetime.datetime]):
            attempt (Union[Unset, int]):  Default: 1.
            cancellation (Union['FleetProfileApplicationCancellationIntent', None, Unset]):
            child_progress (Union['FleetProfileChildProgress', None, Unset]):
            child_source (Union[Literal['switch-adapter'], None, Unset]):
            completed_steps (Union[Unset, int]):  Default: 0.
            current_label (Union[None, Unset, str]):
            intended_profile (Union['FleetProfileIntendedConfiguration', None, Unset]):
            operation_kind (Union[Literal['fleet-profile.apply'], None, Unset]):
            retry_of_application_id (Union[None, Unset, str]):
            step_results (Union[Unset, FleetProfileApplicationProgressStepResults]):
            switch_adapter (Union['FleetProfileSwitchAdapterState', None, Unset]):
            total_steps (Union[Unset, int]):  Default: 0.
            workload_intent_ordinal (Union[None, Unset, int]):
     """

    admission_attempt: Union[Unset, int] = 0
    admission_pending: Union[Unset, bool] = False
    admission_retry_at: Union[None, Unset, datetime.datetime] = UNSET
    attempt: Union[Unset, int] = 1
    cancellation: Union['FleetProfileApplicationCancellationIntent', None, Unset] = UNSET
    child_progress: Union['FleetProfileChildProgress', None, Unset] = UNSET
    child_source: Union[Literal['switch-adapter'], None, Unset] = UNSET
    completed_steps: Union[Unset, int] = 0
    current_label: Union[None, Unset, str] = UNSET
    intended_profile: Union['FleetProfileIntendedConfiguration', None, Unset] = UNSET
    operation_kind: Union[Literal['fleet-profile.apply'], None, Unset] = UNSET
    retry_of_application_id: Union[None, Unset, str] = UNSET
    step_results: Union[Unset, 'FleetProfileApplicationProgressStepResults'] = UNSET
    switch_adapter: Union['FleetProfileSwitchAdapterState', None, Unset] = UNSET
    total_steps: Union[Unset, int] = 0
    workload_intent_ordinal: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
        from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
        admission_attempt = self.admission_attempt

        admission_pending = self.admission_pending

        admission_retry_at: Union[None, Unset, str]
        if isinstance(self.admission_retry_at, Unset):
            admission_retry_at = UNSET
        elif isinstance(self.admission_retry_at, datetime.datetime):
            admission_retry_at = self.admission_retry_at.isoformat()
        else:
            admission_retry_at = self.admission_retry_at

        attempt = self.attempt

        cancellation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationIntent):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        child_progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.child_progress, Unset):
            child_progress = UNSET
        elif isinstance(self.child_progress, FleetProfileChildProgress):
            child_progress = self.child_progress.to_dict()
        else:
            child_progress = self.child_progress

        child_source: Union[Literal['switch-adapter'], None, Unset]
        if isinstance(self.child_source, Unset):
            child_source = UNSET
        else:
            child_source = self.child_source

        completed_steps = self.completed_steps

        current_label: Union[None, Unset, str]
        if isinstance(self.current_label, Unset):
            current_label = UNSET
        else:
            current_label = self.current_label

        intended_profile: Union[None, Unset, dict[str, Any]]
        if isinstance(self.intended_profile, Unset):
            intended_profile = UNSET
        elif isinstance(self.intended_profile, FleetProfileIntendedConfiguration):
            intended_profile = self.intended_profile.to_dict()
        else:
            intended_profile = self.intended_profile

        operation_kind: Union[Literal['fleet-profile.apply'], None, Unset]
        if isinstance(self.operation_kind, Unset):
            operation_kind = UNSET
        else:
            operation_kind = self.operation_kind

        retry_of_application_id: Union[None, Unset, str]
        if isinstance(self.retry_of_application_id, Unset):
            retry_of_application_id = UNSET
        else:
            retry_of_application_id = self.retry_of_application_id

        step_results: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.step_results, Unset):
            step_results = self.step_results.to_dict()

        switch_adapter: Union[None, Unset, dict[str, Any]]
        if isinstance(self.switch_adapter, Unset):
            switch_adapter = UNSET
        elif isinstance(self.switch_adapter, FleetProfileSwitchAdapterState):
            switch_adapter = self.switch_adapter.to_dict()
        else:
            switch_adapter = self.switch_adapter

        total_steps = self.total_steps

        workload_intent_ordinal: Union[None, Unset, int]
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
        if intended_profile is not UNSET:
            field_dict["intended_profile"] = intended_profile
        if operation_kind is not UNSET:
            field_dict["operation_kind"] = operation_kind
        if retry_of_application_id is not UNSET:
            field_dict["retry_of_application_id"] = retry_of_application_id
        if step_results is not UNSET:
            field_dict["step_results"] = step_results
        if switch_adapter is not UNSET:
            field_dict["switch_adapter"] = switch_adapter
        if total_steps is not UNSET:
            field_dict["total_steps"] = total_steps
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_cancellation_intent import FleetProfileApplicationCancellationIntent
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
        from ..models.fleet_profile_intended_configuration import FleetProfileIntendedConfiguration
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
        d = dict(src_dict)
        admission_attempt = d.pop("admission_attempt", UNSET)

        admission_pending = d.pop("admission_pending", UNSET)

        def _parse_admission_retry_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                admission_retry_at_type_0 = isoparse(data)



                return admission_retry_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        admission_retry_at = _parse_admission_retry_at(d.pop("admission_retry_at", UNSET))


        attempt = d.pop("attempt", UNSET)

        def _parse_cancellation(data: object) -> Union['FleetProfileApplicationCancellationIntent', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = FleetProfileApplicationCancellationIntent.from_dict(data)



                return cancellation_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileApplicationCancellationIntent', None, Unset], data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


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


        def _parse_child_source(data: object) -> Union[Literal['switch-adapter'], None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            child_source_type_0 = cast(Literal['switch-adapter'] , data)
            if child_source_type_0 != 'switch-adapter':
                raise ValueError(f"child_source_type_0 must match const 'switch-adapter', got '{child_source_type_0}'")
            return child_source_type_0
            return cast(Union[Literal['switch-adapter'], None, Unset], data)

        child_source = _parse_child_source(d.pop("child_source", UNSET))


        completed_steps = d.pop("completed_steps", UNSET)

        def _parse_current_label(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        current_label = _parse_current_label(d.pop("current_label", UNSET))


        def _parse_intended_profile(data: object) -> Union['FleetProfileIntendedConfiguration', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intended_profile_type_0 = FleetProfileIntendedConfiguration.from_dict(data)



                return intended_profile_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileIntendedConfiguration', None, Unset], data)

        intended_profile = _parse_intended_profile(d.pop("intended_profile", UNSET))


        def _parse_operation_kind(data: object) -> Union[Literal['fleet-profile.apply'], None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            operation_kind_type_0 = cast(Literal['fleet-profile.apply'] , data)
            if operation_kind_type_0 != 'fleet-profile.apply':
                raise ValueError(f"operation_kind_type_0 must match const 'fleet-profile.apply', got '{operation_kind_type_0}'")
            return operation_kind_type_0
            return cast(Union[Literal['fleet-profile.apply'], None, Unset], data)

        operation_kind = _parse_operation_kind(d.pop("operation_kind", UNSET))


        def _parse_retry_of_application_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        retry_of_application_id = _parse_retry_of_application_id(d.pop("retry_of_application_id", UNSET))


        _step_results = d.pop("step_results", UNSET)
        step_results: Union[Unset, FleetProfileApplicationProgressStepResults]
        if isinstance(_step_results,  Unset):
            step_results = UNSET
        else:
            step_results = FleetProfileApplicationProgressStepResults.from_dict(_step_results)




        def _parse_switch_adapter(data: object) -> Union['FleetProfileSwitchAdapterState', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                switch_adapter_type_0 = FleetProfileSwitchAdapterState.from_dict(data)



                return switch_adapter_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileSwitchAdapterState', None, Unset], data)

        switch_adapter = _parse_switch_adapter(d.pop("switch_adapter", UNSET))


        total_steps = d.pop("total_steps", UNSET)

        def _parse_workload_intent_ordinal(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        fleet_profile_application_progress = cls(
            admission_attempt=admission_attempt,
            admission_pending=admission_pending,
            admission_retry_at=admission_retry_at,
            attempt=attempt,
            cancellation=cancellation,
            child_progress=child_progress,
            child_source=child_source,
            completed_steps=completed_steps,
            current_label=current_label,
            intended_profile=intended_profile,
            operation_kind=operation_kind,
            retry_of_application_id=retry_of_application_id,
            step_results=step_results,
            switch_adapter=switch_adapter,
            total_steps=total_steps,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return fleet_profile_application_progress
