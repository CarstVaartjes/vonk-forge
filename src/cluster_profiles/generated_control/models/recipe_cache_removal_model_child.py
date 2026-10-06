from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeCacheRemovalModelChild")



@_attrs_define
class RecipeCacheRemovalModelChild:
    """ One disjoint model-set removal child accepted with the recipe intent.

        Attributes:
            operation_id (str):
            plan_digest (str):
            request_key (str):
            selected_sets (list[str]):
     """

    operation_id: str
    plan_digest: str
    request_key: str
    selected_sets: list[str]





    def to_dict(self) -> dict[str, Any]:
        operation_id = self.operation_id

        plan_digest = self.plan_digest

        request_key = self.request_key

        selected_sets = self.selected_sets




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operation_id": operation_id,
            "plan_digest": plan_digest,
            "request_key": request_key,
            "selected_sets": selected_sets,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        operation_id = d.pop("operation_id")

        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        selected_sets = cast(list[str], d.pop("selected_sets"))


        recipe_cache_removal_model_child = cls(
            operation_id=operation_id,
            plan_digest=plan_digest,
            request_key=request_key,
            selected_sets=selected_sets,
        )

        return recipe_cache_removal_model_child
