from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_capability_inventory_state import check_library_capability_inventory_state
from ..models.library_capability_inventory_state import LibraryCapabilityInventoryState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.library_capability_provenance import LibraryCapabilityProvenance
  from ..models.library_capability_fact import LibraryCapabilityFact
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="LibraryCapabilityInventory")



@_attrs_define
class LibraryCapabilityInventory:
    """ Compare-friendly model or recipe capability assertions with evidence state.

        Attributes:
            facts (Union[Unset, list['LibraryCapabilityFact']]):
            provenance (Union['LibraryCapabilityProvenance', None, Unset]):
            reasons (Union[Unset, list['LibraryProjectionReason']]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            state (Union[Unset, LibraryCapabilityInventoryState]):  Default: 'unknown'.
     """

    facts: Union[Unset, list['LibraryCapabilityFact']] = UNSET
    provenance: Union['LibraryCapabilityProvenance', None, Unset] = UNSET
    reasons: Union[Unset, list['LibraryProjectionReason']] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    state: Union[Unset, LibraryCapabilityInventoryState] = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_capability_provenance import LibraryCapabilityProvenance
        from ..models.library_capability_fact import LibraryCapabilityFact
        from ..models.library_projection_reason import LibraryProjectionReason
        facts: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.facts, Unset):
            facts = []
            for facts_item_data in self.facts:
                facts_item = facts_item_data.to_dict()
                facts.append(facts_item)



        provenance: Union[None, Unset, dict[str, Any]]
        if isinstance(self.provenance, Unset):
            provenance = UNSET
        elif isinstance(self.provenance, LibraryCapabilityProvenance):
            provenance = self.provenance.to_dict()
        else:
            provenance = self.provenance

        reasons: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)



        schema_version = self.schema_version

        state: Union[Unset, str] = UNSET
        if not isinstance(self.state, Unset):
            state = self.state



        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if facts is not UNSET:
            field_dict["facts"] = facts
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_capability_provenance import LibraryCapabilityProvenance
        from ..models.library_capability_fact import LibraryCapabilityFact
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        facts = []
        _facts = d.pop("facts", UNSET)
        for facts_item_data in (_facts or []):
            facts_item = LibraryCapabilityFact.from_dict(facts_item_data)



            facts.append(facts_item)


        def _parse_provenance(data: object) -> Union['LibraryCapabilityProvenance', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                provenance_type_0 = LibraryCapabilityProvenance.from_dict(data)



                return provenance_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryCapabilityProvenance', None, Unset], data)

        provenance = _parse_provenance(d.pop("provenance", UNSET))


        reasons = []
        _reasons = d.pop("reasons", UNSET)
        for reasons_item_data in (_reasons or []):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        _state = d.pop("state", UNSET)
        state: Union[Unset, LibraryCapabilityInventoryState]
        if isinstance(_state,  Unset):
            state = UNSET
        else:
            state = check_library_capability_inventory_state(_state)




        library_capability_inventory = cls(
            facts=facts,
            provenance=provenance,
            reasons=reasons,
            schema_version=schema_version,
            state=state,
        )

        return library_capability_inventory
