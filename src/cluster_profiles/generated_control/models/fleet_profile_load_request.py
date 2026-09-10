from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileLoadRequest")



@_attrs_define
class FleetProfileLoadRequest:
    """
        Attributes:
            dry_run (Union[Unset, bool]):  Default: False.
            request_key (Union[None, Unset, str]):
     """

    dry_run: Union[Unset, bool] = False
    request_key: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        dry_run = self.dry_run

        request_key: Union[None, Unset, str]
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if request_key is not UNSET:
            field_dict["request_key"] = request_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        dry_run = d.pop("dry_run", UNSET)

        def _parse_request_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        fleet_profile_load_request = cls(
            dry_run=dry_run,
            request_key=request_key,
        )

        return fleet_profile_load_request
