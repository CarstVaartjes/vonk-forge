from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_job_interface_adapter import check_recipe_job_interface_adapter
from ..models.recipe_job_interface_adapter import RecipeJobInterfaceAdapter
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_job_input import RecipeJobInput
  from ..models.recipe_job_output import RecipeJobOutput





T = TypeVar("T", bound="RecipeJobInterface")



@_attrs_define
class RecipeJobInterface:
    """
        Attributes:
            adapter (RecipeJobInterfaceAdapter):
            output (RecipeJobOutput):
            input_ (None | RecipeJobInput | Unset):
     """

    adapter: RecipeJobInterfaceAdapter
    output: RecipeJobOutput
    input_: None | RecipeJobInput | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_input import RecipeJobInput # noqa: PLC0415
        from ..models.recipe_job_output import RecipeJobOutput # noqa: PLC0415
        adapter: str = self.adapter

        output = self.output.to_dict()

        input_: dict[str, Any] | None | Unset
        if isinstance(self.input_, Unset):
            input_ = UNSET
        elif isinstance(self.input_, RecipeJobInput):
            input_ = self.input_.to_dict()
        else:
            input_ = self.input_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
            "output": output,
        })
        if input_ is not UNSET:
            field_dict["input"] = input_

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_input import RecipeJobInput # noqa: PLC0415
        from ..models.recipe_job_output import RecipeJobOutput # noqa: PLC0415
        d = dict(src_dict)
        adapter = check_recipe_job_interface_adapter(d.pop("adapter"))




        output = RecipeJobOutput.from_dict(d.pop("output"))




        def _parse_input_(data: object) -> None | RecipeJobInput | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                input_type_0 = RecipeJobInput.from_dict(data)



                return input_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeJobInput | Unset, data)

        input_ = _parse_input_(d.pop("input", UNSET))


        recipe_job_interface = cls(
            adapter=adapter,
            output=output,
            input_=input_,
        )

        return recipe_job_interface
