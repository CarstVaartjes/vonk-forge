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
  from ..models.freshness_evidence import FreshnessEvidence
  from ..models.rollout_preparation import RolloutPreparation
  from ..models.spark_fit import SparkFit
  from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
  from ..models.stop_impact import StopImpact





T = TypeVar("T", bound="RunSwitchAssessment")



@_attrs_define
class RunSwitchAssessment:
    """ Planner-owned admission and observations shared by operator reviews.

        Attributes:
            alias (Union[None, str]):
            allowed (bool):
            blockers (list['RunSwitchReason']):
            fit_after_stop (Union['SparkFit', None]):
            fit_current (SparkFit):
            stops (list['StopImpact']):
            warnings (list['RunSwitchReason']):
            effective_settings (Union['EffectiveSettingsSelection', None, Unset]):
            freshness (Union[Unset, list['FreshnessEvidence']]):
            post_stop_memory_check (Union['ConditionalPostStopMemoryCheck', None, Unset]):
            preparation (Union['RolloutPreparation', None, Unset]):
            stop_before_prepare (Union[Unset, bool]):  Default: False.
            stop_before_transfer (Union[Unset, bool]):  Default: False.
     """

    alias: Union[None, str]
    allowed: bool
    blockers: list['RunSwitchReason']
    fit_after_stop: Union['SparkFit', None]
    fit_current: 'SparkFit'
    stops: list['StopImpact']
    warnings: list['RunSwitchReason']
    effective_settings: Union['EffectiveSettingsSelection', None, Unset] = UNSET
    freshness: Union[Unset, list['FreshnessEvidence']] = UNSET
    post_stop_memory_check: Union['ConditionalPostStopMemoryCheck', None, Unset] = UNSET
    preparation: Union['RolloutPreparation', None, Unset] = UNSET
    stop_before_prepare: Union[Unset, bool] = False
    stop_before_transfer: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.freshness_evidence import FreshnessEvidence
        from ..models.rollout_preparation import RolloutPreparation
        from ..models.spark_fit import SparkFit
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
        from ..models.stop_impact import StopImpact
        alias: Union[None, str]
        alias = self.alias

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        fit_after_stop: Union[None, dict[str, Any]]
        if isinstance(self.fit_after_stop, SparkFit):
            fit_after_stop = self.fit_after_stop.to_dict()
        else:
            fit_after_stop = self.fit_after_stop

        fit_current = self.fit_current.to_dict()

        stops = []
        for stops_item_data in self.stops:
            stops_item = stops_item_data.to_dict()
            stops.append(stops_item)



        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        effective_settings: Union[None, Unset, dict[str, Any]]
        if isinstance(self.effective_settings, Unset):
            effective_settings = UNSET
        elif isinstance(self.effective_settings, EffectiveSettingsSelection):
            effective_settings = self.effective_settings.to_dict()
        else:
            effective_settings = self.effective_settings

        freshness: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.freshness, Unset):
            freshness = []
            for freshness_item_data in self.freshness:
                freshness_item = freshness_item_data.to_dict()
                freshness.append(freshness_item)



        post_stop_memory_check: Union[None, Unset, dict[str, Any]]
        if isinstance(self.post_stop_memory_check, Unset):
            post_stop_memory_check = UNSET
        elif isinstance(self.post_stop_memory_check, ConditionalPostStopMemoryCheck):
            post_stop_memory_check = self.post_stop_memory_check.to_dict()
        else:
            post_stop_memory_check = self.post_stop_memory_check

        preparation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.preparation, Unset):
            preparation = UNSET
        elif isinstance(self.preparation, RolloutPreparation):
            preparation = self.preparation.to_dict()
        else:
            preparation = self.preparation

        stop_before_prepare = self.stop_before_prepare

        stop_before_transfer = self.stop_before_transfer


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "allowed": allowed,
            "blockers": blockers,
            "fit_after_stop": fit_after_stop,
            "fit_current": fit_current,
            "stops": stops,
            "warnings": warnings,
        })
        if effective_settings is not UNSET:
            field_dict["effective_settings"] = effective_settings
        if freshness is not UNSET:
            field_dict["freshness"] = freshness
        if post_stop_memory_check is not UNSET:
            field_dict["post_stop_memory_check"] = post_stop_memory_check
        if preparation is not UNSET:
            field_dict["preparation"] = preparation
        if stop_before_prepare is not UNSET:
            field_dict["stop_before_prepare"] = stop_before_prepare
        if stop_before_transfer is not UNSET:
            field_dict["stop_before_transfer"] = stop_before_transfer

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.freshness_evidence import FreshnessEvidence
        from ..models.rollout_preparation import RolloutPreparation
        from ..models.spark_fit import SparkFit
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
        from ..models.stop_impact import StopImpact
        d = dict(src_dict)
        def _parse_alias(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        alias = _parse_alias(d.pop("alias"))


        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        def _parse_fit_after_stop(data: object) -> Union['SparkFit', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fit_after_stop_type_0 = SparkFit.from_dict(data)



                return fit_after_stop_type_0
            except: # noqa: E722
                pass
            return cast(Union['SparkFit', None], data)

        fit_after_stop = _parse_fit_after_stop(d.pop("fit_after_stop"))


        fit_current = SparkFit.from_dict(d.pop("fit_current"))




        stops = []
        _stops = d.pop("stops")
        for stops_item_data in (_stops):
            stops_item = StopImpact.from_dict(stops_item_data)



            stops.append(stops_item)


        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = RunSwitchReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        def _parse_effective_settings(data: object) -> Union['EffectiveSettingsSelection', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_settings_type_0 = EffectiveSettingsSelection.from_dict(data)



                return effective_settings_type_0
            except: # noqa: E722
                pass
            return cast(Union['EffectiveSettingsSelection', None, Unset], data)

        effective_settings = _parse_effective_settings(d.pop("effective_settings", UNSET))


        freshness = []
        _freshness = d.pop("freshness", UNSET)
        for freshness_item_data in (_freshness or []):
            freshness_item = FreshnessEvidence.from_dict(freshness_item_data)



            freshness.append(freshness_item)


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


        def _parse_preparation(data: object) -> Union['RolloutPreparation', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preparation_type_0 = RolloutPreparation.from_dict(data)



                return preparation_type_0
            except: # noqa: E722
                pass
            return cast(Union['RolloutPreparation', None, Unset], data)

        preparation = _parse_preparation(d.pop("preparation", UNSET))


        stop_before_prepare = d.pop("stop_before_prepare", UNSET)

        stop_before_transfer = d.pop("stop_before_transfer", UNSET)

        run_switch_assessment = cls(
            alias=alias,
            allowed=allowed,
            blockers=blockers,
            fit_after_stop=fit_after_stop,
            fit_current=fit_current,
            stops=stops,
            warnings=warnings,
            effective_settings=effective_settings,
            freshness=freshness,
            post_stop_memory_check=post_stop_memory_check,
            preparation=preparation,
            stop_before_prepare=stop_before_prepare,
            stop_before_transfer=stop_before_transfer,
        )

        return run_switch_assessment
