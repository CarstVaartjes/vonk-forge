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
  from ..models.unavailable_fleet_profile_view import UnavailableFleetProfileView





T = TypeVar("T", bound="FleetProfileList")



@_attrs_define
class FleetProfileList:
    """
        Attributes:
            generated_at (datetime.datetime):
            profiles (list[FleetProfileView | UnavailableFleetProfileView]):
     """

    generated_at: datetime.datetime
    profiles: list[FleetProfileView | UnavailableFleetProfileView]





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_view import FleetProfileView # noqa: PLC0415
        from ..models.unavailable_fleet_profile_view import UnavailableFleetProfileView # noqa: PLC0415
        generated_at = self.generated_at.isoformat()

        profiles = []
        for profiles_item_data in self.profiles:
            profiles_item: dict[str, Any]
            if isinstance(profiles_item_data, FleetProfileView):
                profiles_item = profiles_item_data.to_dict()
            else:
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
        from ..models.unavailable_fleet_profile_view import UnavailableFleetProfileView # noqa: PLC0415
        d = dict(src_dict)
        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        profiles = []
        _profiles = d.pop("profiles")
        for profiles_item_data in (_profiles):
            def _parse_profiles_item(data: object) -> FleetProfileView | UnavailableFleetProfileView:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    profiles_item_type_0 = FleetProfileView.from_dict(data)



                    return profiles_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                profiles_item_type_1 = UnavailableFleetProfileView.from_dict(data)



                return profiles_item_type_1

            profiles_item = _parse_profiles_item(profiles_item_data)

            profiles.append(profiles_item)


        fleet_profile_list = cls(
            generated_at=generated_at,
            profiles=profiles,
        )

        return fleet_profile_list
