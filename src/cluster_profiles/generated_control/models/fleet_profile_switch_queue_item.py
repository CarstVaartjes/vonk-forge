from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_queue_item_kind import check_fleet_profile_switch_queue_item_kind
from ..models.fleet_profile_switch_queue_item_kind import FleetProfileSwitchQueueItemKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope





T = TypeVar("T", bound="FleetProfileSwitchQueueItem")



@_attrs_define
class FleetProfileSwitchQueueItem:
    """ One durable Run/Switch child in the profile reconciliation queue.

        Attributes:
            id (str):
            kind (FleetProfileSwitchQueueItemKind):
            profile_stop_scope (Union['RunSwitchProfileStopScope', None, Unset]):
     """

    id: str
    kind: FleetProfileSwitchQueueItemKind
    profile_stop_scope: Union['RunSwitchProfileStopScope', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope
        id = self.id

        kind: str = self.kind

        profile_stop_scope: Union[None, Unset, dict[str, Any]]
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
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_fleet_profile_switch_queue_item_kind(d.pop("kind"))




        def _parse_profile_stop_scope(data: object) -> Union['RunSwitchProfileStopScope', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_stop_scope_type_0 = RunSwitchProfileStopScope.from_dict(data)



                return profile_stop_scope_type_0
            except: # noqa: E722
                pass
            return cast(Union['RunSwitchProfileStopScope', None, Unset], data)

        profile_stop_scope = _parse_profile_stop_scope(d.pop("profile_stop_scope", UNSET))


        fleet_profile_switch_queue_item = cls(
            id=id,
            kind=kind,
            profile_stop_scope=profile_stop_scope,
        )

        return fleet_profile_switch_queue_item
