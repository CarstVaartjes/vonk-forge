from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_model_lineage_relation import check_library_model_lineage_relation
from ..models.library_model_lineage_relation import LibraryModelLineageRelation
from typing import cast

if TYPE_CHECKING:
  from ..models.library_catalog_reference import LibraryCatalogReference





T = TypeVar("T", bound="LibraryModelLineage")



@_attrs_define
class LibraryModelLineage:
    """
        Attributes:
            derivation (str):
            publisher (str):
            relation (LibraryModelLineageRelation):
            source_model (LibraryCatalogReference): A content addressed catalog identity retained in a Library response.
     """

    derivation: str
    publisher: str
    relation: LibraryModelLineageRelation
    source_model: 'LibraryCatalogReference'





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_catalog_reference import LibraryCatalogReference
        derivation = self.derivation

        publisher = self.publisher

        relation: str = self.relation

        source_model = self.source_model.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "derivation": derivation,
            "publisher": publisher,
            "relation": relation,
            "source_model": source_model,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_catalog_reference import LibraryCatalogReference
        d = dict(src_dict)
        derivation = d.pop("derivation")

        publisher = d.pop("publisher")

        relation = check_library_model_lineage_relation(d.pop("relation"))




        source_model = LibraryCatalogReference.from_dict(d.pop("source_model"))




        library_model_lineage = cls(
            derivation=derivation,
            publisher=publisher,
            relation=relation,
            source_model=source_model,
        )

        return library_model_lineage
