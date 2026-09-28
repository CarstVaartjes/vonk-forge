from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelRecord")



@_attrs_define
class ModelRecord:
    """
        Attributes:
            publisher (str):
            slug (str):
            title (str):
     """

    publisher: str
    slug: str
    title: str





    def to_dict(self) -> dict[str, Any]:
        publisher = self.publisher

        slug = self.slug

        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "publisher": publisher,
            "slug": slug,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        publisher = d.pop("publisher")

        slug = d.pop("slug")

        title = d.pop("title")

        model_record = cls(
            publisher=publisher,
            slug=slug,
            title=title,
        )

        return model_record
