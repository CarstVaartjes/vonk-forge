from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelRecord")



@_attrs_define
class ModelRecord:
    """
        Attributes:
            architecture (str):
            publisher (str):
            slug (str):
            title (str):
     """

    architecture: str
    publisher: str
    slug: str
    title: str





    def to_dict(self) -> dict[str, Any]:
        architecture = self.architecture

        publisher = self.publisher

        slug = self.slug

        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "publisher": publisher,
            "slug": slug,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        architecture = d.pop("architecture")

        publisher = d.pop("publisher")

        slug = d.pop("slug")

        title = d.pop("title")

        model_record = cls(
            architecture=architecture,
            publisher=publisher,
            slug=slug,
            title=title,
        )

        return model_record
