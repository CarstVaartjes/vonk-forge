from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_view import FleetProfileView





T = TypeVar("T", bound="FleetProfileList")



@_attrs_define
class FleetProfileList:
    """
        Attributes:
            generated_at (datetime.datetime):
            profiles (list['FleetProfileView']):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    generated_at: datetime.datetime
    profiles: list['FleetProfileView']
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_view import FleetProfileView
        generated_at = self.generated_at.isoformat()

        profiles = []
        for profiles_item_data in self.profiles:
            profiles_item = profiles_item_data.to_dict()
            profiles.append(profiles_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generated_at": generated_at,
            "profiles": profiles,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_view import FleetProfileView
        d = dict(src_dict)
        generated_at = isoparse(d.pop("generated_at"))




        profiles = []
        _profiles = d.pop("profiles")
        for profiles_item_data in (_profiles):
            profiles_item = FleetProfileView.from_dict(profiles_item_data)



            profiles.append(profiles_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_profile_list = cls(
            generated_at=generated_at,
            profiles=profiles,
            schema_version=schema_version,
        )

        return fleet_profile_list
