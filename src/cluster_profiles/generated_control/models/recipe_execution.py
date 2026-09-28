from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_definition import RecipeBuildDefinition





T = TypeVar("T", bound="RecipeExecution")



@_attrs_define
class RecipeExecution:
    """ The platform builds every recipe image from its pinned base and context.

        Attributes:
            build (RecipeBuildDefinition):
     """

    build: RecipeBuildDefinition





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_definition import RecipeBuildDefinition # noqa: PLC0415
        build = self.build.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build": build,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_definition import RecipeBuildDefinition # noqa: PLC0415
        d = dict(src_dict)
        build = RecipeBuildDefinition.from_dict(d.pop("build"))




        recipe_execution = cls(
            build=build,
        )

        return recipe_execution
