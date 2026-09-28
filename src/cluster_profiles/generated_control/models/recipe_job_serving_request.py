from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots





T = TypeVar("T", bound="RecipeJobServingRequest")



@_attrs_define
class RecipeJobServingRequest:
    """ A job check stages its fixture as the input when the interface has one.

        Attributes:
            fixture (str):
            output_slot (str):
            transport (Literal['job']):
            input_slots (RecipeJobServingRequestInputSlots | Unset):
     """

    fixture: str
    output_slot: str
    transport: Literal['job']
    input_slots: RecipeJobServingRequestInputSlots | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots # noqa: PLC0415
        fixture = self.fixture

        output_slot = self.output_slot

        transport = self.transport

        input_slots: dict[str, Any] | Unset = UNSET
        if not isinstance(self.input_slots, Unset):
            input_slots = self.input_slots.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "fixture": fixture,
            "output_slot": output_slot,
            "transport": transport,
        })
        if input_slots is not UNSET:
            field_dict["input_slots"] = input_slots

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots # noqa: PLC0415
        d = dict(src_dict)
        fixture = d.pop("fixture")

        output_slot = d.pop("output_slot")

        transport = cast(Literal['job'] , d.pop("transport"))
        if transport != 'job':
            raise ValueError(f"transport must match const 'job', got '{transport}'")

        _input_slots = d.pop("input_slots", UNSET)
        input_slots: RecipeJobServingRequestInputSlots | Unset
        if isinstance(_input_slots,  Unset):
            input_slots = UNSET
        else:
            input_slots = RecipeJobServingRequestInputSlots.from_dict(_input_slots)




        recipe_job_serving_request = cls(
            fixture=fixture,
            output_slot=output_slot,
            transport=transport,
            input_slots=input_slots,
        )

        return recipe_job_serving_request
