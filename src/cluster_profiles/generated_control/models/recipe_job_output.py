from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_output_slot import RecipeOutputSlot





T = TypeVar("T", bound="RecipeJobOutput")



@_attrs_define
class RecipeJobOutput:
    """
        Attributes:
            max_total_bytes (int):
            path (Literal['/outputs']):
            slots (list['RecipeOutputSlot']):
     """

    max_total_bytes: int
    path: Literal['/outputs']
    slots: list['RecipeOutputSlot']





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_output_slot import RecipeOutputSlot
        max_total_bytes = self.max_total_bytes

        path = self.path

        slots = []
        for slots_item_data in self.slots:
            slots_item = slots_item_data.to_dict()
            slots.append(slots_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "max_total_bytes": max_total_bytes,
            "path": path,
            "slots": slots,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_output_slot import RecipeOutputSlot
        d = dict(src_dict)
        max_total_bytes = d.pop("max_total_bytes")

        path = cast(Literal['/outputs'] , d.pop("path"))
        if path != '/outputs':
            raise ValueError(f"path must match const '/outputs', got '{path}'")

        slots = []
        _slots = d.pop("slots")
        for slots_item_data in (_slots):
            slots_item = RecipeOutputSlot.from_dict(slots_item_data)



            slots.append(slots_item)


        recipe_job_output = cls(
            max_total_bytes=max_total_bytes,
            path=path,
            slots=slots,
        )

        return recipe_job_output
