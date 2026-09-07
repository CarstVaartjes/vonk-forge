from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_child_progress_phase import check_fleet_profile_child_progress_phase
from ..models.fleet_profile_child_progress_phase import FleetProfileChildProgressPhase
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileChildProgress")



@_attrs_define
class FleetProfileChildProgress:
    """ Typed progress emitted by the profile-owned Run switch adapter.

        Attributes:
            phase (FleetProfileChildProgressPhase):
            bytes_ (Union[None, Unset, int]):
            node_ids (Union[Unset, list[str]]):
            total_bytes (Union[None, Unset, int]):
     """

    phase: FleetProfileChildProgressPhase
    bytes_: Union[None, Unset, int] = UNSET
    node_ids: Union[Unset, list[str]] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        phase: str = self.phase

        bytes_: Union[None, Unset, int]
        if isinstance(self.bytes_, Unset):
            bytes_ = UNSET
        else:
            bytes_ = self.bytes_

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
        })
        if bytes_ is not UNSET:
            field_dict["bytes"] = bytes_
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        phase = check_fleet_profile_child_progress_phase(d.pop("phase"))




        def _parse_bytes_(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        bytes_ = _parse_bytes_(d.pop("bytes", UNSET))


        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        fleet_profile_child_progress = cls(
            phase=phase,
            bytes_=bytes_,
            node_ids=node_ids,
            total_bytes=total_bytes,
        )

        return fleet_profile_child_progress
