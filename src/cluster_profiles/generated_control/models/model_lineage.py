from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_lineage_relation import check_model_lineage_relation
from ..models.model_lineage_relation import ModelLineageRelation
from typing import cast

if TYPE_CHECKING:
  from ..models.model_lineage_source import ModelLineageSource





T = TypeVar("T", bound="ModelLineage")



@_attrs_define
class ModelLineage:
    """
        Attributes:
            derivation (str):
            publisher (str):
            relation (ModelLineageRelation):
            source_model (ModelLineageSource):
     """

    derivation: str
    publisher: str
    relation: ModelLineageRelation
    source_model: 'ModelLineageSource'





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_lineage_source import ModelLineageSource
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
        from ..models.model_lineage_source import ModelLineageSource
        d = dict(src_dict)
        derivation = d.pop("derivation")

        publisher = d.pop("publisher")

        relation = check_model_lineage_relation(d.pop("relation"))




        source_model = ModelLineageSource.from_dict(d.pop("source_model"))




        model_lineage = cls(
            derivation=derivation,
            publisher=publisher,
            relation=relation,
            source_model=source_model,
        )

        return model_lineage
