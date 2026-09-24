from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_cancellation_view_cause import check_fleet_profile_application_cancellation_view_cause
from ..models.fleet_profile_application_cancellation_view_cause import FleetProfileApplicationCancellationViewCause
from ..models.fleet_profile_application_cancellation_view_state import check_fleet_profile_application_cancellation_view_state
from ..models.fleet_profile_application_cancellation_view_state import FleetProfileApplicationCancellationViewState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_effect import FleetProfileApplicationEffect





T = TypeVar("T", bound="FleetProfileApplicationCancellationView")



@_attrs_define
class FleetProfileApplicationCancellationView:
    """ Live projection of completed, pending, and unissued cancelled effects.

        Attributes:
            actor (str):
            cancelled_effects (list['FleetProfileApplicationEffect']):
            cause (FleetProfileApplicationCancellationViewCause):
            completed_effects (list['FleetProfileApplicationEffect']):
            pending_effects (list['FleetProfileApplicationEffect']):
            request_key (str):
            requested_at (datetime.datetime):
            state (FleetProfileApplicationCancellationViewState):
            deadline_at (Union[None, Unset, datetime.datetime]):
            dependency (Union[None, Unset, str]):
            owner (Union[None, Unset, str]):
     """

    actor: str
    cancelled_effects: list['FleetProfileApplicationEffect']
    cause: FleetProfileApplicationCancellationViewCause
    completed_effects: list['FleetProfileApplicationEffect']
    pending_effects: list['FleetProfileApplicationEffect']
    request_key: str
    requested_at: datetime.datetime
    state: FleetProfileApplicationCancellationViewState
    deadline_at: Union[None, Unset, datetime.datetime] = UNSET
    dependency: Union[None, Unset, str] = UNSET
    owner: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_effect import FleetProfileApplicationEffect
        actor = self.actor

        cancelled_effects = []
        for cancelled_effects_item_data in self.cancelled_effects:
            cancelled_effects_item = cancelled_effects_item_data.to_dict()
            cancelled_effects.append(cancelled_effects_item)



        cause: str = self.cause

        completed_effects = []
        for completed_effects_item_data in self.completed_effects:
            completed_effects_item = completed_effects_item_data.to_dict()
            completed_effects.append(completed_effects_item)



        pending_effects = []
        for pending_effects_item_data in self.pending_effects:
            pending_effects_item = pending_effects_item_data.to_dict()
            pending_effects.append(pending_effects_item)



        request_key = self.request_key

        requested_at = self.requested_at.isoformat()

        state: str = self.state

        deadline_at: Union[None, Unset, str]
        if isinstance(self.deadline_at, Unset):
            deadline_at = UNSET
        elif isinstance(self.deadline_at, datetime.datetime):
            deadline_at = self.deadline_at.isoformat()
        else:
            deadline_at = self.deadline_at

        dependency: Union[None, Unset, str]
        if isinstance(self.dependency, Unset):
            dependency = UNSET
        else:
            dependency = self.dependency

        owner: Union[None, Unset, str]
        if isinstance(self.owner, Unset):
            owner = UNSET
        else:
            owner = self.owner


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "cancelled_effects": cancelled_effects,
            "cause": cause,
            "completed_effects": completed_effects,
            "pending_effects": pending_effects,
            "request_key": request_key,
            "requested_at": requested_at,
            "state": state,
        })
        if deadline_at is not UNSET:
            field_dict["deadline_at"] = deadline_at
        if dependency is not UNSET:
            field_dict["dependency"] = dependency
        if owner is not UNSET:
            field_dict["owner"] = owner

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_effect import FleetProfileApplicationEffect
        d = dict(src_dict)
        actor = d.pop("actor")

        cancelled_effects = []
        _cancelled_effects = d.pop("cancelled_effects")
        for cancelled_effects_item_data in (_cancelled_effects):
            cancelled_effects_item = FleetProfileApplicationEffect.from_dict(cancelled_effects_item_data)



            cancelled_effects.append(cancelled_effects_item)


        cause = check_fleet_profile_application_cancellation_view_cause(d.pop("cause"))




        completed_effects = []
        _completed_effects = d.pop("completed_effects")
        for completed_effects_item_data in (_completed_effects):
            completed_effects_item = FleetProfileApplicationEffect.from_dict(completed_effects_item_data)



            completed_effects.append(completed_effects_item)


        pending_effects = []
        _pending_effects = d.pop("pending_effects")
        for pending_effects_item_data in (_pending_effects):
            pending_effects_item = FleetProfileApplicationEffect.from_dict(pending_effects_item_data)



            pending_effects.append(pending_effects_item)


        request_key = d.pop("request_key")

        requested_at = isoparse(d.pop("requested_at"))




        state = check_fleet_profile_application_cancellation_view_state(d.pop("state"))




        def _parse_deadline_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                deadline_at_type_0 = isoparse(data)



                return deadline_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        deadline_at = _parse_deadline_at(d.pop("deadline_at", UNSET))


        def _parse_dependency(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        dependency = _parse_dependency(d.pop("dependency", UNSET))


        def _parse_owner(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        owner = _parse_owner(d.pop("owner", UNSET))


        fleet_profile_application_cancellation_view = cls(
            actor=actor,
            cancelled_effects=cancelled_effects,
            cause=cause,
            completed_effects=completed_effects,
            pending_effects=pending_effects,
            request_key=request_key,
            requested_at=requested_at,
            state=state,
            deadline_at=deadline_at,
            dependency=dependency,
            owner=owner,
        )

        return fleet_profile_application_cancellation_view
