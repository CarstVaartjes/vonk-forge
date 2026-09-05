from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="TelemetryDetails")



@_attrs_define
class TelemetryDetails:
    """
        Attributes:
            accelerator_name (Union[None, Unset, str]):
            accelerator_performance_state (Union[None, Unset, str]):
     """

    accelerator_name: Union[None, Unset, str] = UNSET
    accelerator_performance_state: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        accelerator_name: Union[None, Unset, str]
        if isinstance(self.accelerator_name, Unset):
            accelerator_name = UNSET
        else:
            accelerator_name = self.accelerator_name

        accelerator_performance_state: Union[None, Unset, str]
        if isinstance(self.accelerator_performance_state, Unset):
            accelerator_performance_state = UNSET
        else:
            accelerator_performance_state = self.accelerator_performance_state


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if accelerator_name is not UNSET:
            field_dict["accelerator_name"] = accelerator_name
        if accelerator_performance_state is not UNSET:
            field_dict["accelerator_performance_state"] = accelerator_performance_state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_accelerator_name(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        accelerator_name = _parse_accelerator_name(d.pop("accelerator_name", UNSET))


        def _parse_accelerator_performance_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        accelerator_performance_state = _parse_accelerator_performance_state(d.pop("accelerator_performance_state", UNSET))


        telemetry_details = cls(
            accelerator_name=accelerator_name,
            accelerator_performance_state=accelerator_performance_state,
        )

        return telemetry_details
