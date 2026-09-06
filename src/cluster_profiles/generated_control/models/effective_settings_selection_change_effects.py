from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.effective_settings_selection_change_effects_additional_property import check_effective_settings_selection_change_effects_additional_property
from ..models.effective_settings_selection_change_effects_additional_property import EffectiveSettingsSelectionChangeEffectsAdditionalProperty
from typing import cast






T = TypeVar("T", bound="EffectiveSettingsSelectionChangeEffects")



@_attrs_define
class EffectiveSettingsSelectionChangeEffects:
    """
     """

    additional_properties: dict[str, EffectiveSettingsSelectionChangeEffectsAdditionalProperty] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        for prop_name, prop in self.additional_properties.items():
            field_dict[prop_name] = prop


        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        effective_settings_selection_change_effects = cls(
        )


        additional_properties = {}
        for prop_name, prop_dict in d.items():
            additional_property = check_effective_settings_selection_change_effects_additional_property(prop_dict)



            additional_properties[prop_name] = additional_property

        effective_settings_selection_change_effects.additional_properties = additional_properties
        return effective_settings_selection_change_effects

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> EffectiveSettingsSelectionChangeEffectsAdditionalProperty:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: EffectiveSettingsSelectionChangeEffectsAdditionalProperty) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
