from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_installation_effect_action import check_fleet_profile_installation_effect_action
from ..models.fleet_profile_installation_effect_action import FleetProfileInstallationEffectAction
from typing import cast






T = TypeVar("T", bound="FleetProfileInstallationEffect")



@_attrs_define
class FleetProfileInstallationEffect:
    """
        Attributes:
            action (FleetProfileInstallationEffectAction):
            installation_id (str):
            node_ids (list[str]):
     """

    action: FleetProfileInstallationEffectAction
    installation_id: str
    node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        action: str = self.action

        installation_id = self.installation_id

        node_ids = self.node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "installation_id": installation_id,
            "node_ids": node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = check_fleet_profile_installation_effect_action(d.pop("action"))




        installation_id = d.pop("installation_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        fleet_profile_installation_effect = cls(
            action=action,
            installation_id=installation_id,
            node_ids=node_ids,
        )

        return fleet_profile_installation_effect
