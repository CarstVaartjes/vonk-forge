from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_run_effect_action import check_fleet_profile_run_effect_action
from ..models.fleet_profile_run_effect_action import FleetProfileRunEffectAction
from typing import cast






T = TypeVar("T", bound="FleetProfileRunEffect")



@_attrs_define
class FleetProfileRunEffect:
    """
        Attributes:
            action (FleetProfileRunEffectAction):
            alias (str):
            installation_id (str):
            node_ids (list[str]):
            run_id (str):
     """

    action: FleetProfileRunEffectAction
    alias: str
    installation_id: str
    node_ids: list[str]
    run_id: str





    def to_dict(self) -> dict[str, Any]:
        action: str = self.action

        alias = self.alias

        installation_id = self.installation_id

        node_ids = self.node_ids



        run_id = self.run_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "alias": alias,
            "installation_id": installation_id,
            "node_ids": node_ids,
            "run_id": run_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = check_fleet_profile_run_effect_action(d.pop("action"))




        alias = d.pop("alias")

        installation_id = d.pop("installation_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        run_id = d.pop("run_id")

        fleet_profile_run_effect = cls(
            action=action,
            alias=alias,
            installation_id=installation_id,
            node_ids=node_ids,
            run_id=run_id,
        )

        return fleet_profile_run_effect
