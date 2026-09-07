from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileAssignmentContext")



@_attrs_define
class FleetProfileAssignmentContext:
    """ Persisted mapping and installation identities for one assignment.

        Attributes:
            installation_id (Union[None, Unset, str]):
            mapping_generation (Union[None, Unset, int]):
            mapping_id (Union[None, Unset, str]):
     """

    installation_id: Union[None, Unset, str] = UNSET
    mapping_generation: Union[None, Unset, int] = UNSET
    mapping_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        installation_id: Union[None, Unset, str]
        if isinstance(self.installation_id, Unset):
            installation_id = UNSET
        else:
            installation_id = self.installation_id

        mapping_generation: Union[None, Unset, int]
        if isinstance(self.mapping_generation, Unset):
            mapping_generation = UNSET
        else:
            mapping_generation = self.mapping_generation

        mapping_id: Union[None, Unset, str]
        if isinstance(self.mapping_id, Unset):
            mapping_id = UNSET
        else:
            mapping_id = self.mapping_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if installation_id is not UNSET:
            field_dict["installation_id"] = installation_id
        if mapping_generation is not UNSET:
            field_dict["mapping_generation"] = mapping_generation
        if mapping_id is not UNSET:
            field_dict["mapping_id"] = mapping_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_installation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        installation_id = _parse_installation_id(d.pop("installation_id", UNSET))


        def _parse_mapping_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        mapping_generation = _parse_mapping_generation(d.pop("mapping_generation", UNSET))


        def _parse_mapping_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        mapping_id = _parse_mapping_id(d.pop("mapping_id", UNSET))


        fleet_profile_assignment_context = cls(
            installation_id=installation_id,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
        )

        return fleet_profile_assignment_context
