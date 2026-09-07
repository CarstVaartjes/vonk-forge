from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_progress_child_source_type_0 import check_fleet_profile_application_progress_child_source_type_0
from ..models.fleet_profile_application_progress_child_source_type_0 import FleetProfileApplicationProgressChildSourceType0
from ..models.fleet_profile_application_progress_operation_kind_type_0 import check_fleet_profile_application_progress_operation_kind_type_0
from ..models.fleet_profile_application_progress_operation_kind_type_0 import FleetProfileApplicationProgressOperationKindType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_child_progress import FleetProfileChildProgress
  from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
  from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
  from ..models.fleet_profile_library_placement_context import FleetProfileLibraryPlacementContext
  from ..models.fleet_profile_application_progress_assignments import FleetProfileApplicationProgressAssignments





T = TypeVar("T", bound="FleetProfileApplicationProgress")



@_attrs_define
class FleetProfileApplicationProgress:
    """ Typed progress tree persisted with every profile application.

        Attributes:
            assignments (Union[Unset, FleetProfileApplicationProgressAssignments]):
            child_progress (Union['FleetProfileChildProgress', None, Unset]):
            child_source (Union[FleetProfileApplicationProgressChildSourceType0, None, Unset]):
            completed_steps (Union[Unset, int]):  Default: 0.
            current_label (Union[None, Unset, str]):
            library_placement (Union['FleetProfileLibraryPlacementContext', None, Unset]):
            operation_kind (Union[FleetProfileApplicationProgressOperationKindType0, None, Unset]):
            step_results (Union[Unset, FleetProfileApplicationProgressStepResults]):
            switch_adapter (Union['FleetProfileSwitchAdapterState', None, Unset]):
            total_steps (Union[Unset, int]):  Default: 0.
     """

    assignments: Union[Unset, 'FleetProfileApplicationProgressAssignments'] = UNSET
    child_progress: Union['FleetProfileChildProgress', None, Unset] = UNSET
    child_source: Union[FleetProfileApplicationProgressChildSourceType0, None, Unset] = UNSET
    completed_steps: Union[Unset, int] = 0
    current_label: Union[None, Unset, str] = UNSET
    library_placement: Union['FleetProfileLibraryPlacementContext', None, Unset] = UNSET
    operation_kind: Union[FleetProfileApplicationProgressOperationKindType0, None, Unset] = UNSET
    step_results: Union[Unset, 'FleetProfileApplicationProgressStepResults'] = UNSET
    switch_adapter: Union['FleetProfileSwitchAdapterState', None, Unset] = UNSET
    total_steps: Union[Unset, int] = 0





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
        from ..models.fleet_profile_library_placement_context import FleetProfileLibraryPlacementContext
        from ..models.fleet_profile_application_progress_assignments import FleetProfileApplicationProgressAssignments
        assignments: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.assignments, Unset):
            assignments = self.assignments.to_dict()

        child_progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.child_progress, Unset):
            child_progress = UNSET
        elif isinstance(self.child_progress, FleetProfileChildProgress):
            child_progress = self.child_progress.to_dict()
        else:
            child_progress = self.child_progress

        child_source: Union[None, Unset, str]
        if isinstance(self.child_source, Unset):
            child_source = UNSET
        elif isinstance(self.child_source, str):
            child_source = self.child_source
        else:
            child_source = self.child_source

        completed_steps = self.completed_steps

        current_label: Union[None, Unset, str]
        if isinstance(self.current_label, Unset):
            current_label = UNSET
        else:
            current_label = self.current_label

        library_placement: Union[None, Unset, dict[str, Any]]
        if isinstance(self.library_placement, Unset):
            library_placement = UNSET
        elif isinstance(self.library_placement, FleetProfileLibraryPlacementContext):
            library_placement = self.library_placement.to_dict()
        else:
            library_placement = self.library_placement

        operation_kind: Union[None, Unset, str]
        if isinstance(self.operation_kind, Unset):
            operation_kind = UNSET
        elif isinstance(self.operation_kind, str):
            operation_kind = self.operation_kind
        else:
            operation_kind = self.operation_kind

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


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if assignments is not UNSET:
            field_dict["assignments"] = assignments
        if child_progress is not UNSET:
            field_dict["child_progress"] = child_progress
        if child_source is not UNSET:
            field_dict["child_source"] = child_source
        if completed_steps is not UNSET:
            field_dict["completed_steps"] = completed_steps
        if current_label is not UNSET:
            field_dict["current_label"] = current_label
        if library_placement is not UNSET:
            field_dict["library_placement"] = library_placement
        if operation_kind is not UNSET:
            field_dict["operation_kind"] = operation_kind
        if step_results is not UNSET:
            field_dict["step_results"] = step_results
        if switch_adapter is not UNSET:
            field_dict["switch_adapter"] = switch_adapter
        if total_steps is not UNSET:
            field_dict["total_steps"] = total_steps

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_child_progress import FleetProfileChildProgress
        from ..models.fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
        from ..models.fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
        from ..models.fleet_profile_library_placement_context import FleetProfileLibraryPlacementContext
        from ..models.fleet_profile_application_progress_assignments import FleetProfileApplicationProgressAssignments
        d = dict(src_dict)
        _assignments = d.pop("assignments", UNSET)
        assignments: Union[Unset, FleetProfileApplicationProgressAssignments]
        if isinstance(_assignments,  Unset):
            assignments = UNSET
        else:
            assignments = FleetProfileApplicationProgressAssignments.from_dict(_assignments)




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


        def _parse_child_source(data: object) -> Union[FleetProfileApplicationProgressChildSourceType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                child_source_type_0 = check_fleet_profile_application_progress_child_source_type_0(data)



                return child_source_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileApplicationProgressChildSourceType0, None, Unset], data)

        child_source = _parse_child_source(d.pop("child_source", UNSET))


        completed_steps = d.pop("completed_steps", UNSET)

        def _parse_current_label(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        current_label = _parse_current_label(d.pop("current_label", UNSET))


        def _parse_library_placement(data: object) -> Union['FleetProfileLibraryPlacementContext', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                library_placement_type_0 = FleetProfileLibraryPlacementContext.from_dict(data)



                return library_placement_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileLibraryPlacementContext', None, Unset], data)

        library_placement = _parse_library_placement(d.pop("library_placement", UNSET))


        def _parse_operation_kind(data: object) -> Union[FleetProfileApplicationProgressOperationKindType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                operation_kind_type_0 = check_fleet_profile_application_progress_operation_kind_type_0(data)



                return operation_kind_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileApplicationProgressOperationKindType0, None, Unset], data)

        operation_kind = _parse_operation_kind(d.pop("operation_kind", UNSET))


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

        fleet_profile_application_progress = cls(
            assignments=assignments,
            child_progress=child_progress,
            child_source=child_source,
            completed_steps=completed_steps,
            current_label=current_label,
            library_placement=library_placement,
            operation_kind=operation_kind,
            step_results=step_results,
            switch_adapter=switch_adapter,
            total_steps=total_steps,
        )

        return fleet_profile_application_progress
