from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_view import FleetProfileView





T = TypeVar("T", bound="FleetProfileList")



@_attrs_define
class FleetProfileList:
    """
        Attributes:
            generated_at (datetime.datetime):
            profiles (list[FleetProfileView]):
     """

    generated_at: datetime.datetime
    profiles: list[FleetProfileView]





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_view import FleetProfileView # noqa: PLC0415
        generated_at = self.generated_at.isoformat()

        profiles = []
        for profiles_item_data in self.profiles:
            profiles_item = profiles_item_data.to_dict()
            profiles.append(profiles_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generated_at": generated_at,
            "profiles": profiles,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_view import FleetProfileView # noqa: PLC0415
        d = dict(src_dict)
        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        profiles = []
        _profiles = d.pop("profiles")
        for profiles_item_data in (_profiles):
            profiles_item = FleetProfileView.from_dict(profiles_item_data)



            profiles.append(profiles_item)


        fleet_profile_list = cls(
            generated_at=generated_at,
            profiles=profiles,
        )

        return fleet_profile_list
