from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="LibraryModelIdentity")



@_attrs_define
class LibraryModelIdentity:
    """ Content-addressed identity for a canonical Model document.

        Attributes:
            content_sha256 (str):
            publisher (str):
            slug (str):
            kind (Union[Literal['model'], Unset]):  Default: 'model'.
     """

    content_sha256: str
    publisher: str
    slug: str
    kind: Union[Literal['model'], Unset] = 'model'





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        publisher = self.publisher

        slug = self.slug

        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "publisher": publisher,
            "slug": slug,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        publisher = d.pop("publisher")

        slug = d.pop("slug")

        kind = cast(Union[Literal['model'], Unset] , d.pop("kind", UNSET))
        if kind != 'model' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'model', got '{kind}'")

        library_model_identity = cls(
            content_sha256=content_sha256,
            publisher=publisher,
            slug=slug,
            kind=kind,
        )

        return library_model_identity
