from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_job_interface_adapter import check_recipe_job_interface_adapter
from ..models.recipe_job_interface_adapter import RecipeJobInterfaceAdapter
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

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
            path (Literal['/outputs']):
            input_ (Union['RecipeJobInput', None, Unset]):
     """

    adapter: RecipeJobInterfaceAdapter
    output: 'RecipeJobOutput'
    path: Literal['/outputs']
    input_: Union['RecipeJobInput', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_input import RecipeJobInput
        from ..models.recipe_job_output import RecipeJobOutput
        adapter: str = self.adapter

        output = self.output.to_dict()

        path = self.path

        input_: Union[None, Unset, dict[str, Any]]
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
            "path": path,
        })
        if input_ is not UNSET:
            field_dict["input"] = input_

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_input import RecipeJobInput
        from ..models.recipe_job_output import RecipeJobOutput
        d = dict(src_dict)
        adapter = check_recipe_job_interface_adapter(d.pop("adapter"))




        output = RecipeJobOutput.from_dict(d.pop("output"))




        path = cast(Literal['/outputs'] , d.pop("path"))
        if path != '/outputs':
            raise ValueError(f"path must match const '/outputs', got '{path}'")

        def _parse_input_(data: object) -> Union['RecipeJobInput', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                input_type_0 = RecipeJobInput.from_dict(data)



                return input_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeJobInput', None, Unset], data)

        input_ = _parse_input_(d.pop("input", UNSET))


        recipe_job_interface = cls(
            adapter=adapter,
            output=output,
            path=path,
            input_=input_,
        )

        return recipe_job_interface
