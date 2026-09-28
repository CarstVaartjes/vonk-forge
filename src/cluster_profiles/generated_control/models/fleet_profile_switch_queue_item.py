from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_queue_item_kind import check_fleet_profile_switch_queue_item_kind
from ..models.fleet_profile_switch_queue_item_kind import FleetProfileSwitchQueueItemKind
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope





T = TypeVar("T", bound="FleetProfileSwitchQueueItem")



@_attrs_define
class FleetProfileSwitchQueueItem:
    """ One durable Run/Switch child in the profile reconciliation queue.

        Attributes:
            id (str):
            kind (FleetProfileSwitchQueueItemKind):
            profile_stop_scope (None | RunSwitchProfileStopScope | Unset):
     """

    id: str
    kind: FleetProfileSwitchQueueItemKind
    profile_stop_scope: None | RunSwitchProfileStopScope | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        id = self.id

        kind: str = self.kind

        profile_stop_scope: dict[str, Any] | None | Unset
        if isinstance(self.profile_stop_scope, Unset):
            profile_stop_scope = UNSET
        elif isinstance(self.profile_stop_scope, RunSwitchProfileStopScope):
            profile_stop_scope = self.profile_stop_scope.to_dict()
        else:
            profile_stop_scope = self.profile_stop_scope


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
        })
        if profile_stop_scope is not UNSET:
            field_dict["profile_stop_scope"] = profile_stop_scope

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_fleet_profile_switch_queue_item_kind(d.pop("kind"))




        def _parse_profile_stop_scope(data: object) -> None | RunSwitchProfileStopScope | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_stop_scope_type_0 = RunSwitchProfileStopScope.from_dict(data)



                return profile_stop_scope_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchProfileStopScope | Unset, data)

        profile_stop_scope = _parse_profile_stop_scope(d.pop("profile_stop_scope", UNSET))


        fleet_profile_switch_queue_item = cls(
            id=id,
            kind=kind,
            profile_stop_scope=profile_stop_scope,
        )

        return fleet_profile_switch_queue_item
