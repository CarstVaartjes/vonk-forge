from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_cancellation_intent_cause import check_fleet_profile_application_cancellation_intent_cause
from ..models.fleet_profile_application_cancellation_intent_cause import FleetProfileApplicationCancellationIntentCause
from ..models.fleet_profile_application_cancellation_intent_state import check_fleet_profile_application_cancellation_intent_state
from ..models.fleet_profile_application_cancellation_intent_state import FleetProfileApplicationCancellationIntentState
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="FleetProfileApplicationCancellationIntent")



@_attrs_define
class FleetProfileApplicationCancellationIntent:
    """ Durable identity and authority for an explicit or superseding cancel.

        Attributes:
            actor (str):
            cause (FleetProfileApplicationCancellationIntentCause):
            request_key (str):
            requested_at (datetime.datetime):
            observation_deadline_at (datetime.datetime | None | Unset):
            observation_due_at (datetime.datetime | None | Unset):
            pending_operation_ids (list[str] | Unset):
            state (FleetProfileApplicationCancellationIntentState | Unset):  Default: 'observing'.
            successor_application_id (None | str | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    actor: str
    cause: FleetProfileApplicationCancellationIntentCause
    request_key: str
    requested_at: datetime.datetime
    observation_deadline_at: datetime.datetime | None | Unset = UNSET
    observation_due_at: datetime.datetime | None | Unset = UNSET
    pending_operation_ids: list[str] | Unset = UNSET
    state: FleetProfileApplicationCancellationIntentState | Unset = 'observing'
    successor_application_id: None | str | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        cause: str = self.cause

        request_key = self.request_key

        requested_at = self.requested_at.isoformat()

        observation_deadline_at: None | str | Unset
        if isinstance(self.observation_deadline_at, Unset):
            observation_deadline_at = UNSET
        elif isinstance(self.observation_deadline_at, datetime.datetime):
            observation_deadline_at = self.observation_deadline_at.isoformat()
        else:
            observation_deadline_at = self.observation_deadline_at

        observation_due_at: None | str | Unset
        if isinstance(self.observation_due_at, Unset):
            observation_due_at = UNSET
        elif isinstance(self.observation_due_at, datetime.datetime):
            observation_due_at = self.observation_due_at.isoformat()
        else:
            observation_due_at = self.observation_due_at

        pending_operation_ids: list[str] | Unset = UNSET
        if not isinstance(self.pending_operation_ids, Unset):
            pending_operation_ids = self.pending_operation_ids



        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state


        successor_application_id: None | str | Unset
        if isinstance(self.successor_application_id, Unset):
            successor_application_id = UNSET
        else:
            successor_application_id = self.successor_application_id

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "cause": cause,
            "request_key": request_key,
            "requested_at": requested_at,
        })
        if observation_deadline_at is not UNSET:
            field_dict["observation_deadline_at"] = observation_deadline_at
        if observation_due_at is not UNSET:
            field_dict["observation_due_at"] = observation_due_at
        if pending_operation_ids is not UNSET:
            field_dict["pending_operation_ids"] = pending_operation_ids
        if state is not UNSET:
            field_dict["state"] = state
        if successor_application_id is not UNSET:
            field_dict["successor_application_id"] = successor_application_id
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        cause = check_fleet_profile_application_cancellation_intent_cause(d.pop("cause"))




        request_key = d.pop("request_key")

        requested_at = datetime.datetime.fromisoformat(d.pop("requested_at"))




        def _parse_observation_deadline_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observation_deadline_at_type_0 = datetime.datetime.fromisoformat(data)



                return observation_deadline_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        observation_deadline_at = _parse_observation_deadline_at(d.pop("observation_deadline_at", UNSET))


        def _parse_observation_due_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observation_due_at_type_0 = datetime.datetime.fromisoformat(data)



                return observation_due_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        observation_due_at = _parse_observation_due_at(d.pop("observation_due_at", UNSET))


        pending_operation_ids = cast(list[str], d.pop("pending_operation_ids", UNSET))


        _state = d.pop("state", UNSET)
        state: FleetProfileApplicationCancellationIntentState | Unset
        if isinstance(_state,  Unset):
            state = UNSET
        else:
            state = check_fleet_profile_application_cancellation_intent_state(_state)




        def _parse_successor_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        successor_application_id = _parse_successor_application_id(d.pop("successor_application_id", UNSET))


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        fleet_profile_application_cancellation_intent = cls(
            actor=actor,
            cause=cause,
            request_key=request_key,
            requested_at=requested_at,
            observation_deadline_at=observation_deadline_at,
            observation_due_at=observation_due_at,
            pending_operation_ids=pending_operation_ids,
            state=state,
            successor_application_id=successor_application_id,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return fleet_profile_application_cancellation_intent
