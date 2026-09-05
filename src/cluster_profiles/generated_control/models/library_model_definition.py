from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.library_model_metadata import LibraryModelMetadata
  from ..models.library_catalog_reference import LibraryCatalogReference





T = TypeVar("T", bound="LibraryModelDefinition")



@_attrs_define
class LibraryModelDefinition:
    """
        Attributes:
            architecture (str):
            identity (LibraryCatalogReference): A content addressed catalog identity retained in a Library response.
            metadata (LibraryModelMetadata):
            model_group (LibraryCatalogReference): A content addressed catalog identity retained in a Library response.
     """

    architecture: str
    identity: 'LibraryCatalogReference'
    metadata: 'LibraryModelMetadata'
    model_group: 'LibraryCatalogReference'





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_catalog_reference import LibraryCatalogReference
        architecture = self.architecture

        identity = self.identity.to_dict()

        metadata = self.metadata.to_dict()

        model_group = self.model_group.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "identity": identity,
            "metadata": metadata,
            "model_group": model_group,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_catalog_reference import LibraryCatalogReference
        d = dict(src_dict)
        architecture = d.pop("architecture")

        identity = LibraryCatalogReference.from_dict(d.pop("identity"))




        metadata = LibraryModelMetadata.from_dict(d.pop("metadata"))




        model_group = LibraryCatalogReference.from_dict(d.pop("model_group"))




        library_model_definition = cls(
            architecture=architecture,
            identity=identity,
            metadata=metadata,
            model_group=model_group,
        )

        return library_model_definition
