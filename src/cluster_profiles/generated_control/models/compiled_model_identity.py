from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledModelIdentity")



@_attrs_define
class CompiledModelIdentity:
    """
        Attributes:
            content_sha256 (str):
            publisher (str):
            slug (str):
     """

    content_sha256: str
    publisher: str
    slug: str





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        publisher = self.publisher

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "publisher": publisher,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        publisher = d.pop("publisher")

        slug = d.pop("slug")

        compiled_model_identity = cls(
            content_sha256=content_sha256,
            publisher=publisher,
            slug=slug,
        )

        return compiled_model_identity
