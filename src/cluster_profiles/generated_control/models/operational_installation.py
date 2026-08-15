from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operational_installation_state import check_operational_installation_state
from ..models.operational_installation_state import OperationalInstallationState
from typing import cast






T = TypeVar("T", bound="OperationalInstallation")



@_attrs_define
class OperationalInstallation:
    """
        Attributes:
            installation_id (str):
            mapping_id (str):
            node_ids (list[str]):
            recipe_build_id (str):
            recipe_revision_id (str):
            state (OperationalInstallationState):
     """

    installation_id: str
    mapping_id: str
    node_ids: list[str]
    recipe_build_id: str
    recipe_revision_id: str
    state: OperationalInstallationState





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        mapping_id = self.mapping_id

        node_ids = self.node_ids



        recipe_build_id = self.recipe_build_id

        recipe_revision_id = self.recipe_revision_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "node_ids": node_ids,
            "recipe_build_id": recipe_build_id,
            "recipe_revision_id": recipe_revision_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        mapping_id = d.pop("mapping_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        recipe_build_id = d.pop("recipe_build_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        state = check_operational_installation_state(d.pop("state"))




        operational_installation = cls(
            installation_id=installation_id,
            mapping_id=mapping_id,
            node_ids=node_ids,
            recipe_build_id=recipe_build_id,
            recipe_revision_id=recipe_revision_id,
            state=state,
        )

        return operational_installation
