from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ModelVersionIdentity")



@_attrs_define
class ModelVersionIdentity:
    """
        Attributes:
            content_sha256 (str):
            kind (Literal['model-version']):
            publisher (str):
            slug (str):
     """

    content_sha256: str
    kind: Literal['model-version']
    publisher: str
    slug: str





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        kind = self.kind

        publisher = self.publisher

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "kind": kind,
            "publisher": publisher,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        kind = cast(Literal['model-version'] , d.pop("kind"))
        if kind != 'model-version':
            raise ValueError(f"kind must match const 'model-version', got '{kind}'")

        publisher = d.pop("publisher")

        slug = d.pop("slug")

        model_version_identity = cls(
            content_sha256=content_sha256,
            kind=kind,
            publisher=publisher,
            slug=slug,
        )

        return model_version_identity
