from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelCacheRepairCheckpoint")



@_attrs_define
class ModelCacheRepairCheckpoint:
    """
        Attributes:
            completed_objects (list[str]):
            transfer_id (str):
     """

    completed_objects: list[str]
    transfer_id: str





    def to_dict(self) -> dict[str, Any]:
        completed_objects = self.completed_objects



        transfer_id = self.transfer_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_objects": completed_objects,
            "transfer_id": transfer_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        completed_objects = cast(list[str], d.pop("completed_objects"))


        transfer_id = d.pop("transfer_id")

        model_cache_repair_checkpoint = cls(
            completed_objects=completed_objects,
            transfer_id=transfer_id,
        )

        return model_cache_repair_checkpoint
