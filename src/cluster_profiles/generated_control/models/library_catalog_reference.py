from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_catalog_reference_kind import check_library_catalog_reference_kind
from ..models.library_catalog_reference_kind import LibraryCatalogReferenceKind
from typing import cast






T = TypeVar("T", bound="LibraryCatalogReference")



@_attrs_define
class LibraryCatalogReference:
    """ A content addressed catalog identity retained in a Library response.

        Attributes:
            content_sha256 (str):
            kind (LibraryCatalogReferenceKind):
            publisher (str):
            slug (str):
     """

    content_sha256: str
    kind: LibraryCatalogReferenceKind
    publisher: str
    slug: str





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        kind: str = self.kind

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

        kind = check_library_catalog_reference_kind(d.pop("kind"))




        publisher = d.pop("publisher")

        slug = d.pop("slug")

        library_catalog_reference = cls(
            content_sha256=content_sha256,
            kind=kind,
            publisher=publisher,
            slug=slug,
        )

        return library_catalog_reference
