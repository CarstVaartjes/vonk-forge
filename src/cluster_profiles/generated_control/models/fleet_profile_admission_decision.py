from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.effective_settings_selection import EffectiveSettingsSelection
  from ..models.run_switch_reason import RunSwitchReason
  from ..models.fleet_profile_resource_requirement import FleetProfileResourceRequirement
  from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
  from ..models.stop_impact import StopImpact





T = TypeVar("T", bound="FleetProfileAdmissionDecision")



@_attrs_define
class FleetProfileAdmissionDecision:
    """
        Attributes:
            alias (Union[None, str]):
            allowed (bool):
            assignment_id (str):
            blockers (list['RunSwitchReason']):
            effective_settings (Union['EffectiveSettingsSelection', None]):
            requirements (list['FleetProfileResourceRequirement']):
            stop_before_prepare (bool):
            stop_before_transfer (bool):
            stops (list['StopImpact']):
            post_stop_memory_check (Union['ConditionalPostStopMemoryCheck', None, Unset]):
     """

    alias: Union[None, str]
    allowed: bool
    assignment_id: str
    blockers: list['RunSwitchReason']
    effective_settings: Union['EffectiveSettingsSelection', None]
    requirements: list['FleetProfileResourceRequirement']
    stop_before_prepare: bool
    stop_before_transfer: bool
    stops: list['StopImpact']
    post_stop_memory_check: Union['ConditionalPostStopMemoryCheck', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.fleet_profile_resource_requirement import FleetProfileResourceRequirement
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
        from ..models.stop_impact import StopImpact
        alias: Union[None, str]
        alias = self.alias

        allowed = self.allowed

        assignment_id = self.assignment_id

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        effective_settings: Union[None, dict[str, Any]]
        if isinstance(self.effective_settings, EffectiveSettingsSelection):
            effective_settings = self.effective_settings.to_dict()
        else:
            effective_settings = self.effective_settings

        requirements = []
        for requirements_item_data in self.requirements:
            requirements_item = requirements_item_data.to_dict()
            requirements.append(requirements_item)



        stop_before_prepare = self.stop_before_prepare

        stop_before_transfer = self.stop_before_transfer

        stops = []
        for stops_item_data in self.stops:
            stops_item = stops_item_data.to_dict()
            stops.append(stops_item)



        post_stop_memory_check: Union[None, Unset, dict[str, Any]]
        if isinstance(self.post_stop_memory_check, Unset):
            post_stop_memory_check = UNSET
        elif isinstance(self.post_stop_memory_check, ConditionalPostStopMemoryCheck):
            post_stop_memory_check = self.post_stop_memory_check.to_dict()
        else:
            post_stop_memory_check = self.post_stop_memory_check


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "allowed": allowed,
            "assignment_id": assignment_id,
            "blockers": blockers,
            "effective_settings": effective_settings,
            "requirements": requirements,
            "stop_before_prepare": stop_before_prepare,
            "stop_before_transfer": stop_before_transfer,
            "stops": stops,
        })
        if post_stop_memory_check is not UNSET:
            field_dict["post_stop_memory_check"] = post_stop_memory_check

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.fleet_profile_resource_requirement import FleetProfileResourceRequirement
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
        from ..models.stop_impact import StopImpact
        d = dict(src_dict)
        def _parse_alias(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        alias = _parse_alias(d.pop("alias"))


        allowed = d.pop("allowed")

        assignment_id = d.pop("assignment_id")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        def _parse_effective_settings(data: object) -> Union['EffectiveSettingsSelection', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_settings_type_0 = EffectiveSettingsSelection.from_dict(data)



                return effective_settings_type_0
            except: # noqa: E722
                pass
            return cast(Union['EffectiveSettingsSelection', None], data)

        effective_settings = _parse_effective_settings(d.pop("effective_settings"))


        requirements = []
        _requirements = d.pop("requirements")
        for requirements_item_data in (_requirements):
            requirements_item = FleetProfileResourceRequirement.from_dict(requirements_item_data)



            requirements.append(requirements_item)


        stop_before_prepare = d.pop("stop_before_prepare")

        stop_before_transfer = d.pop("stop_before_transfer")

        stops = []
        _stops = d.pop("stops")
        for stops_item_data in (_stops):
            stops_item = StopImpact.from_dict(stops_item_data)



            stops.append(stops_item)


        def _parse_post_stop_memory_check(data: object) -> Union['ConditionalPostStopMemoryCheck', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                post_stop_memory_check_type_0 = ConditionalPostStopMemoryCheck.from_dict(data)



                return post_stop_memory_check_type_0
            except: # noqa: E722
                pass
            return cast(Union['ConditionalPostStopMemoryCheck', None, Unset], data)

        post_stop_memory_check = _parse_post_stop_memory_check(d.pop("post_stop_memory_check", UNSET))


        fleet_profile_admission_decision = cls(
            alias=alias,
            allowed=allowed,
            assignment_id=assignment_id,
            blockers=blockers,
            effective_settings=effective_settings,
            requirements=requirements,
            stop_before_prepare=stop_before_prepare,
            stop_before_transfer=stop_before_transfer,
            stops=stops,
            post_stop_memory_check=post_stop_memory_check,
        )

        return fleet_profile_admission_decision
