from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.visual_output_slot import VisualOutputSlot





T = TypeVar("T", bound="VisualInterfaceOutput")



@_attrs_define
class VisualInterfaceOutput:
    """
        Attributes:
            allowed_media_types (list[str]):
            path (str):
            max_total_bytes (Union[None, Unset, int]):
            slots (Union[Unset, list['VisualOutputSlot']]):
     """

    allowed_media_types: list[str]
    path: str
    max_total_bytes: Union[None, Unset, int] = UNSET
    slots: Union[Unset, list['VisualOutputSlot']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_output_slot import VisualOutputSlot
        allowed_media_types = self.allowed_media_types



        path = self.path

        max_total_bytes: Union[None, Unset, int]
        if isinstance(self.max_total_bytes, Unset):
            max_total_bytes = UNSET
        else:
            max_total_bytes = self.max_total_bytes

        slots: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.slots, Unset):
            slots = []
            for slots_item_data in self.slots:
                slots_item = slots_item_data.to_dict()
                slots.append(slots_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed_media_types": allowed_media_types,
            "path": path,
        })
        if max_total_bytes is not UNSET:
            field_dict["max_total_bytes"] = max_total_bytes
        if slots is not UNSET:
            field_dict["slots"] = slots

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_output_slot import VisualOutputSlot
        d = dict(src_dict)
        allowed_media_types = cast(list[str], d.pop("allowed_media_types"))


        path = d.pop("path")

        def _parse_max_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        max_total_bytes = _parse_max_total_bytes(d.pop("max_total_bytes", UNSET))


        slots = []
        _slots = d.pop("slots", UNSET)
        for slots_item_data in (_slots or []):
            slots_item = VisualOutputSlot.from_dict(slots_item_data)



            slots.append(slots_item)


        visual_interface_output = cls(
            allowed_media_types=allowed_media_types,
            path=path,
            max_total_bytes=max_total_bytes,
            slots=slots,
        )

        return visual_interface_output
