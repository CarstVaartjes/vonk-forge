from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_input_slot import RecipeInputSlot





T = TypeVar("T", bound="RecipeJobInput")



@_attrs_define
class RecipeJobInput:
    """
        Attributes:
            max_bytes (int):
            media_types (list[str]):
            path (Literal['/inputs']):
            required (bool):
            slots (Union[None, Unset, list['RecipeInputSlot']]):
     """

    max_bytes: int
    media_types: list[str]
    path: Literal['/inputs']
    required: bool
    slots: Union[None, Unset, list['RecipeInputSlot']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_input_slot import RecipeInputSlot
        max_bytes = self.max_bytes

        media_types = self.media_types



        path = self.path

        required = self.required

        slots: Union[None, Unset, list[dict[str, Any]]]
        if isinstance(self.slots, Unset):
            slots = UNSET
        elif isinstance(self.slots, list):
            slots = []
            for slots_type_0_item_data in self.slots:
                slots_type_0_item = slots_type_0_item_data.to_dict()
                slots.append(slots_type_0_item)


        else:
            slots = self.slots


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "max_bytes": max_bytes,
            "media_types": media_types,
            "path": path,
            "required": required,
        })
        if slots is not UNSET:
            field_dict["slots"] = slots

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_input_slot import RecipeInputSlot
        d = dict(src_dict)
        max_bytes = d.pop("max_bytes")

        media_types = cast(list[str], d.pop("media_types"))


        path = cast(Literal['/inputs'] , d.pop("path"))
        if path != '/inputs':
            raise ValueError(f"path must match const '/inputs', got '{path}'")

        required = d.pop("required")

        def _parse_slots(data: object) -> Union[None, Unset, list['RecipeInputSlot']]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                slots_type_0 = []
                _slots_type_0 = data
                for slots_type_0_item_data in (_slots_type_0):
                    slots_type_0_item = RecipeInputSlot.from_dict(slots_type_0_item_data)



                    slots_type_0.append(slots_type_0_item)

                return slots_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, list['RecipeInputSlot']], data)

        slots = _parse_slots(d.pop("slots", UNSET))


        recipe_job_input = cls(
            max_bytes=max_bytes,
            media_types=media_types,
            path=path,
            required=required,
            slots=slots,
        )

        return recipe_job_input
