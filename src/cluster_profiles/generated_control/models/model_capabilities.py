from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.model_capability_provenance import ModelCapabilityProvenance
  from ..models.model_capability_fact import ModelCapabilityFact





T = TypeVar("T", bound="ModelCapabilities")



@_attrs_define
class ModelCapabilities:
    """
        Attributes:
            facts (list['ModelCapabilityFact']):
            provenance (ModelCapabilityProvenance):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    facts: list['ModelCapabilityFact']
    provenance: 'ModelCapabilityProvenance'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_capability_provenance import ModelCapabilityProvenance
        from ..models.model_capability_fact import ModelCapabilityFact
        facts = []
        for facts_item_data in self.facts:
            facts_item = facts_item_data.to_dict()
            facts.append(facts_item)



        provenance = self.provenance.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "facts": facts,
            "provenance": provenance,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_capability_provenance import ModelCapabilityProvenance
        from ..models.model_capability_fact import ModelCapabilityFact
        d = dict(src_dict)
        facts = []
        _facts = d.pop("facts")
        for facts_item_data in (_facts):
            facts_item = ModelCapabilityFact.from_dict(facts_item_data)



            facts.append(facts_item)


        provenance = ModelCapabilityProvenance.from_dict(d.pop("provenance"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_capabilities = cls(
            facts=facts,
            provenance=provenance,
            schema_version=schema_version,
        )

        return model_capabilities
