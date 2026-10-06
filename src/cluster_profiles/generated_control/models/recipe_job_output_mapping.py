from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeJobOutputMapping")



@_attrs_define
class RecipeJobOutputMapping:
    """
        Attributes:
            extensions (list[str]):
            media_type (str):
            slot (str):
     """

    extensions: list[str]
    media_type: str
    slot: str





    def to_dict(self) -> dict[str, Any]:
        extensions = self.extensions



        media_type = self.media_type

        slot = self.slot


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "extensions": extensions,
            "media_type": media_type,
            "slot": slot,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        extensions = cast(list[str], d.pop("extensions"))


        media_type = d.pop("media_type")

        slot = d.pop("slot")

        recipe_job_output_mapping = cls(
            extensions=extensions,
            media_type=media_type,
            slot=slot,
        )

        return recipe_job_output_mapping
