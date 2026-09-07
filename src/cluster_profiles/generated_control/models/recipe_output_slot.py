from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeOutputSlot")



@_attrs_define
class RecipeOutputSlot:
    """
        Attributes:
            description (str):
            extensions (list[str]):
            id (str):
            label (str):
            max_file_bytes (int):
            max_files (int):
            max_total_bytes (int):
            media_types (list[str]):
            min_files (int):
     """

    description: str
    extensions: list[str]
    id: str
    label: str
    max_file_bytes: int
    max_files: int
    max_total_bytes: int
    media_types: list[str]
    min_files: int





    def to_dict(self) -> dict[str, Any]:
        description = self.description

        extensions = self.extensions



        id = self.id

        label = self.label

        max_file_bytes = self.max_file_bytes

        max_files = self.max_files

        max_total_bytes = self.max_total_bytes

        media_types = self.media_types



        min_files = self.min_files


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "extensions": extensions,
            "id": id,
            "label": label,
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
            "media_types": media_types,
            "min_files": min_files,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        extensions = cast(list[str], d.pop("extensions"))


        id = d.pop("id")

        label = d.pop("label")

        max_file_bytes = d.pop("max_file_bytes")

        max_files = d.pop("max_files")

        max_total_bytes = d.pop("max_total_bytes")

        media_types = cast(list[str], d.pop("media_types"))


        min_files = d.pop("min_files")

        recipe_output_slot = cls(
            description=description,
            extensions=extensions,
            id=id,
            label=label,
            max_file_bytes=max_file_bytes,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            media_types=media_types,
            min_files=min_files,
        )

        return recipe_output_slot
