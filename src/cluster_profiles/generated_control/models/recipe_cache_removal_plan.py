from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_cache_removal_intent import RecipeCacheRemovalIntent
  from ..models.recipe_cache_removal_model_child import RecipeCacheRemovalModelChild





T = TypeVar("T", bound="RecipeCacheRemovalPlan")



@_attrs_define
class RecipeCacheRemovalPlan:
    """ Immutable exact targets bound to the current request owner.

        Attributes:
            image_archives (list[str]):
            intent (RecipeCacheRemovalIntent): Exact accepted removal request stored on its existing Job owner.
            model_children (list[RecipeCacheRemovalModelChild]):
            schema_version (Literal[2]):
     """

    image_archives: list[str]
    intent: RecipeCacheRemovalIntent
    model_children: list[RecipeCacheRemovalModelChild]
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_cache_removal_intent import RecipeCacheRemovalIntent # noqa: PLC0415
        from ..models.recipe_cache_removal_model_child import RecipeCacheRemovalModelChild # noqa: PLC0415
        image_archives = self.image_archives



        intent = self.intent.to_dict()

        model_children = []
        for model_children_item_data in self.model_children:
            model_children_item = model_children_item_data.to_dict()
            model_children.append(model_children_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_archives": image_archives,
            "intent": intent,
            "model_children": model_children,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_cache_removal_intent import RecipeCacheRemovalIntent # noqa: PLC0415
        from ..models.recipe_cache_removal_model_child import RecipeCacheRemovalModelChild # noqa: PLC0415
        d = dict(src_dict)
        image_archives = cast(list[str], d.pop("image_archives"))


        intent = RecipeCacheRemovalIntent.from_dict(d.pop("intent"))




        model_children = []
        _model_children = d.pop("model_children")
        for model_children_item_data in (_model_children):
            model_children_item = RecipeCacheRemovalModelChild.from_dict(model_children_item_data)



            model_children.append(model_children_item)


        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_cache_removal_plan = cls(
            image_archives=image_archives,
            intent=intent,
            model_children=model_children,
            schema_version=schema_version,
        )

        return recipe_cache_removal_plan
