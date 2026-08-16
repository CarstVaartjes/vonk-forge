from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.revise_catalog_entity_request_document import ReviseCatalogEntityRequestDocument





T = TypeVar("T", bound="ReviseCatalogEntityRequest")



@_attrs_define
class ReviseCatalogEntityRequest:
    """
        Attributes:
            document (ReviseCatalogEntityRequestDocument):
            expected_revision (int):
     """

    document: 'ReviseCatalogEntityRequestDocument'
    expected_revision: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.revise_catalog_entity_request_document import ReviseCatalogEntityRequestDocument
        document = self.document.to_dict()

        expected_revision = self.expected_revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "expected_revision": expected_revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.revise_catalog_entity_request_document import ReviseCatalogEntityRequestDocument
        d = dict(src_dict)
        document = ReviseCatalogEntityRequestDocument.from_dict(d.pop("document"))




        expected_revision = d.pop("expected_revision")

        revise_catalog_entity_request = cls(
            document=document,
            expected_revision=expected_revision,
        )

        return revise_catalog_entity_request
