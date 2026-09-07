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
  from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots





T = TypeVar("T", bound="RecipeJobServingRequest")



@_attrs_define
class RecipeJobServingRequest:
    """
        Attributes:
            fixture (str):
            output_path (Literal['/outputs']):
            output_slot (str):
            transport (Literal['job']):
            input_path (Union[Literal['/inputs'], None, Unset]):
            input_slots (Union[Unset, RecipeJobServingRequestInputSlots]):
     """

    fixture: str
    output_path: Literal['/outputs']
    output_slot: str
    transport: Literal['job']
    input_path: Union[Literal['/inputs'], None, Unset] = UNSET
    input_slots: Union[Unset, 'RecipeJobServingRequestInputSlots'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots
        fixture = self.fixture

        output_path = self.output_path

        output_slot = self.output_slot

        transport = self.transport

        input_path: Union[Literal['/inputs'], None, Unset]
        if isinstance(self.input_path, Unset):
            input_path = UNSET
        else:
            input_path = self.input_path

        input_slots: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.input_slots, Unset):
            input_slots = self.input_slots.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "fixture": fixture,
            "output_path": output_path,
            "output_slot": output_slot,
            "transport": transport,
        })
        if input_path is not UNSET:
            field_dict["input_path"] = input_path
        if input_slots is not UNSET:
            field_dict["input_slots"] = input_slots

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots
        d = dict(src_dict)
        fixture = d.pop("fixture")

        output_path = cast(Literal['/outputs'] , d.pop("output_path"))
        if output_path != '/outputs':
            raise ValueError(f"output_path must match const '/outputs', got '{output_path}'")

        output_slot = d.pop("output_slot")

        transport = cast(Literal['job'] , d.pop("transport"))
        if transport != 'job':
            raise ValueError(f"transport must match const 'job', got '{transport}'")

        def _parse_input_path(data: object) -> Union[Literal['/inputs'], None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            input_path_type_0 = cast(Literal['/inputs'] , data)
            if input_path_type_0 != '/inputs':
                raise ValueError(f"input_path_type_0 must match const '/inputs', got '{input_path_type_0}'")
            return input_path_type_0
            return cast(Union[Literal['/inputs'], None, Unset], data)

        input_path = _parse_input_path(d.pop("input_path", UNSET))


        _input_slots = d.pop("input_slots", UNSET)
        input_slots: Union[Unset, RecipeJobServingRequestInputSlots]
        if isinstance(_input_slots,  Unset):
            input_slots = UNSET
        else:
            input_slots = RecipeJobServingRequestInputSlots.from_dict(_input_slots)




        recipe_job_serving_request = cls(
            fixture=fixture,
            output_path=output_path,
            output_slot=output_slot,
            transport=transport,
            input_path=input_path,
            input_slots=input_slots,
        )

        return recipe_job_serving_request
