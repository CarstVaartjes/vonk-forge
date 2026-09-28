from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RecipeRunPayload")



@_attrs_define
class RecipeRunPayload:
    """
        Attributes:
            alias (str):
            entity_id (str):
            entity_kind (Literal['recipe-run']):
            installation_id (str):
            mapping_generation (int):
            mapping_id (str):
            route_state (str):
            state (str):
     """

    alias: str
    entity_id: str
    entity_kind: Literal['recipe-run']
    installation_id: str
    mapping_generation: int
    mapping_id: str
    route_state: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        alias = self.alias

        entity_id = self.entity_id

        entity_kind = self.entity_kind

        installation_id = self.installation_id

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        route_state = self.route_state

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "installation_id": installation_id,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "route_state": route_state,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alias = d.pop("alias")

        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['recipe-run'] , d.pop("entity_kind"))
        if entity_kind != 'recipe-run':
            raise ValueError(f"entity_kind must match const 'recipe-run', got '{entity_kind}'")

        installation_id = d.pop("installation_id")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        route_state = d.pop("route_state")

        state = d.pop("state")

        recipe_run_payload = cls(
            alias=alias,
            entity_id=entity_id,
            entity_kind=entity_kind,
            installation_id=installation_id,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            route_state=route_state,
            state=state,
        )

        return recipe_run_payload
