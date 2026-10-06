from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_build_intent_kind import check_recipe_build_intent_kind
from ..models.recipe_build_intent_kind import RecipeBuildIntentKind
from typing import cast






T = TypeVar("T", bound="RecipeBuildIntent")



@_attrs_define
class RecipeBuildIntent:
    """ The accepted producer's intent, independent of its current consumers.

        Attributes:
            kind (RecipeBuildIntentKind):
     """

    kind: RecipeBuildIntentKind





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_recipe_build_intent_kind(d.pop("kind"))




        recipe_build_intent = cls(
            kind=kind,
        )

        return recipe_build_intent
