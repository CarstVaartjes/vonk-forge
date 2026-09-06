from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelMetadata")



@_attrs_define
class ModelMetadata:
    """
        Attributes:
            description (str):
            tags (list[str]):
     """

    description: str
    tags: list[str]





    def to_dict(self) -> dict[str, Any]:
        description = self.description

        tags = self.tags




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "tags": tags,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        tags = cast(list[str], d.pop("tags"))


        model_metadata = cls(
            description=description,
            tags=tags,
        )

        return model_metadata
