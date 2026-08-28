from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.visual_input_slot import VisualInputSlot





T = TypeVar("T", bound="VisualInterfaceInput")



@_attrs_define
class VisualInterfaceInput:
    """
        Attributes:
            max_bytes (int):
            max_files (int):
            media_types (list[str]):
            min_files (int):
            path (str):
            required (bool):
            slots (Union[Unset, list['VisualInputSlot']]):
     """

    max_bytes: int
    max_files: int
    media_types: list[str]
    min_files: int
    path: str
    required: bool
    slots: Union[Unset, list['VisualInputSlot']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_input_slot import VisualInputSlot
        max_bytes = self.max_bytes

        max_files = self.max_files

        media_types = self.media_types



        min_files = self.min_files

        path = self.path

        required = self.required

        slots: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.slots, Unset):
            slots = []
            for slots_item_data in self.slots:
                slots_item = slots_item_data.to_dict()
                slots.append(slots_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "max_bytes": max_bytes,
            "max_files": max_files,
            "media_types": media_types,
            "min_files": min_files,
            "path": path,
            "required": required,
        })
        if slots is not UNSET:
            field_dict["slots"] = slots

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_input_slot import VisualInputSlot
        d = dict(src_dict)
        max_bytes = d.pop("max_bytes")

        max_files = d.pop("max_files")

        media_types = cast(list[str], d.pop("media_types"))


        min_files = d.pop("min_files")

        path = d.pop("path")

        required = d.pop("required")

        slots = []
        _slots = d.pop("slots", UNSET)
        for slots_item_data in (_slots or []):
            slots_item = VisualInputSlot.from_dict(slots_item_data)



            slots.append(slots_item)


        visual_interface_input = cls(
            max_bytes=max_bytes,
            max_files=max_files,
            media_types=media_types,
            min_files=min_files,
            path=path,
            required=required,
            slots=slots,
        )

        return visual_interface_input
