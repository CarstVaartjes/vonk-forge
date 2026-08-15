from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.create_catalog_entity_request_document import CreateCatalogEntityRequestDocument





T = TypeVar("T", bound="CreateCatalogEntityRequest")



@_attrs_define
class CreateCatalogEntityRequest:
    """
        Attributes:
            document (CreateCatalogEntityRequestDocument):
     """

    document: 'CreateCatalogEntityRequestDocument'





    def to_dict(self) -> dict[str, Any]:
        from ..models.create_catalog_entity_request_document import CreateCatalogEntityRequestDocument
        document = self.document.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_catalog_entity_request_document import CreateCatalogEntityRequestDocument
        d = dict(src_dict)
        document = CreateCatalogEntityRequestDocument.from_dict(d.pop("document"))




        create_catalog_entity_request = cls(
            document=document,
        )

        return create_catalog_entity_request
