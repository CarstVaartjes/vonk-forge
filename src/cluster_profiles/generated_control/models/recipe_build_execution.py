from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_build_definition import RecipeBuildDefinition





T = TypeVar("T", bound="RecipeBuildExecution")



@_attrs_define
class RecipeBuildExecution:
    """
        Attributes:
            build (RecipeBuildDefinition):
            mode (Literal['build']):
     """

    build: 'RecipeBuildDefinition'
    mode: Literal['build']





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_definition import RecipeBuildDefinition
        build = self.build.to_dict()

        mode = self.mode


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build": build,
            "mode": mode,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_definition import RecipeBuildDefinition
        d = dict(src_dict)
        build = RecipeBuildDefinition.from_dict(d.pop("build"))




        mode = cast(Literal['build'] , d.pop("mode"))
        if mode != 'build':
            raise ValueError(f"mode must match const 'build', got '{mode}'")

        recipe_build_execution = cls(
            build=build,
            mode=mode,
        )

        return recipe_build_execution
