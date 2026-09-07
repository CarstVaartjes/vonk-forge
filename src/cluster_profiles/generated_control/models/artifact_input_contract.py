from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.artifact_slot_contract import ArtifactSlotContract





T = TypeVar("T", bound="ArtifactInputContract")



@_attrs_define
class ArtifactInputContract:
    """
        Attributes:
            max_bytes (int):
            media_types (list[str]):
            required (bool):
            slots (list['ArtifactSlotContract']):
     """

    max_bytes: int
    media_types: list[str]
    required: bool
    slots: list['ArtifactSlotContract']





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_slot_contract import ArtifactSlotContract
        max_bytes = self.max_bytes

        media_types = self.media_types



        required = self.required

        slots = []
        for slots_item_data in self.slots:
            slots_item = slots_item_data.to_dict()
            slots.append(slots_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "max_bytes": max_bytes,
            "media_types": media_types,
            "required": required,
            "slots": slots,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_slot_contract import ArtifactSlotContract
        d = dict(src_dict)
        max_bytes = d.pop("max_bytes")

        media_types = cast(list[str], d.pop("media_types"))


        required = d.pop("required")

        slots = []
        _slots = d.pop("slots")
        for slots_item_data in (_slots):
            slots_item = ArtifactSlotContract.from_dict(slots_item_data)



            slots.append(slots_item)


        artifact_input_contract = cls(
            max_bytes=max_bytes,
            media_types=media_types,
            required=required,
            slots=slots,
        )

        return artifact_input_contract
