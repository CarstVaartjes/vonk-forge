from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_pending_effect_kind import check_fleet_profile_pending_effect_kind
from ..models.fleet_profile_pending_effect_kind import FleetProfilePendingEffectKind
from typing import cast






T = TypeVar("T", bound="FleetProfilePendingEffect")



@_attrs_define
class FleetProfilePendingEffect:
    """
        Attributes:
            id (str):
            kind (FleetProfilePendingEffectKind):
            node_ids (list[str]):
     """

    id: str
    kind: FleetProfilePendingEffectKind
    node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        kind: str = self.kind

        node_ids = self.node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
            "node_ids": node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_fleet_profile_pending_effect_kind(d.pop("kind"))




        node_ids = cast(list[str], d.pop("node_ids"))


        fleet_profile_pending_effect = cls(
            id=id,
            kind=kind,
            node_ids=node_ids,
        )

        return fleet_profile_pending_effect
