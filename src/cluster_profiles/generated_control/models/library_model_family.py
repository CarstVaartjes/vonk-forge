from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.library_model_metadata import LibraryModelMetadata
  from ..models.library_catalog_reference import LibraryCatalogReference





T = TypeVar("T", bound="LibraryModelFamily")



@_attrs_define
class LibraryModelFamily:
    """
        Attributes:
            family (str):
            identity (LibraryCatalogReference): A content addressed catalog identity retained in a Library response.
            metadata (LibraryModelMetadata):
     """

    family: str
    identity: 'LibraryCatalogReference'
    metadata: 'LibraryModelMetadata'





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_catalog_reference import LibraryCatalogReference
        family = self.family

        identity = self.identity.to_dict()

        metadata = self.metadata.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "identity": identity,
            "metadata": metadata,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_catalog_reference import LibraryCatalogReference
        d = dict(src_dict)
        family = d.pop("family")

        identity = LibraryCatalogReference.from_dict(d.pop("identity"))




        metadata = LibraryModelMetadata.from_dict(d.pop("metadata"))




        library_model_family = cls(
            family=family,
            identity=identity,
            metadata=metadata,
        )

        return library_model_family
