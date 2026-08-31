from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelDeletionNodeImpactResponse")



@_attrs_define
class ModelDeletionNodeImpactResponse:
    """
        Attributes:
            installation_ids (list[str]):
            installed_bytes (int):
            node_id (str):
            recipe_ids (list[str]):
     """

    installation_ids: list[str]
    installed_bytes: int
    node_id: str
    recipe_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        installation_ids = self.installation_ids



        installed_bytes = self.installed_bytes

        node_id = self.node_id

        recipe_ids = self.recipe_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_ids": installation_ids,
            "installed_bytes": installed_bytes,
            "node_id": node_id,
            "recipe_ids": recipe_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_ids = cast(list[str], d.pop("installation_ids"))


        installed_bytes = d.pop("installed_bytes")

        node_id = d.pop("node_id")

        recipe_ids = cast(list[str], d.pop("recipe_ids"))


        model_deletion_node_impact_response = cls(
            installation_ids=installation_ids,
            installed_bytes=installed_bytes,
            node_id=node_id,
            recipe_ids=recipe_ids,
        )

        return model_deletion_node_impact_response
