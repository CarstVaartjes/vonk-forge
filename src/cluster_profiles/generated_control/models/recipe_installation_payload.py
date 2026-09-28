from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RecipeInstallationPayload")



@_attrs_define
class RecipeInstallationPayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['recipe-installation']):
            mapping_generation (int):
            mapping_id (str):
            recipe_revision_id (str):
            state (str):
     """

    entity_id: str
    entity_kind: Literal['recipe-installation']
    mapping_generation: int
    mapping_id: str
    recipe_revision_id: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        recipe_revision_id = self.recipe_revision_id

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "recipe_revision_id": recipe_revision_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['recipe-installation'] , d.pop("entity_kind"))
        if entity_kind != 'recipe-installation':
            raise ValueError(f"entity_kind must match const 'recipe-installation', got '{entity_kind}'")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        state = d.pop("state")

        recipe_installation_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            recipe_revision_id=recipe_revision_id,
            state=state,
        )

        return recipe_installation_payload
