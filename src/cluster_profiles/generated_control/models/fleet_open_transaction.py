from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetOpenTransaction")



@_attrs_define
class FleetOpenTransaction:
    """
        Attributes:
            transaction_age_seconds (float):
            application_name (None | str | Unset):
            query (None | str | Unset):
            state (None | str | Unset):
     """

    transaction_age_seconds: float
    application_name: None | str | Unset = UNSET
    query: None | str | Unset = UNSET
    state: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        transaction_age_seconds = self.transaction_age_seconds

        application_name: None | str | Unset
        if isinstance(self.application_name, Unset):
            application_name = UNSET
        else:
            application_name = self.application_name

        query: None | str | Unset
        if isinstance(self.query, Unset):
            query = UNSET
        else:
            query = self.query

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "transaction_age_seconds": transaction_age_seconds,
        })
        if application_name is not UNSET:
            field_dict["application_name"] = application_name
        if query is not UNSET:
            field_dict["query"] = query
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        transaction_age_seconds = d.pop("transaction_age_seconds")

        def _parse_application_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        application_name = _parse_application_name(d.pop("application_name", UNSET))


        def _parse_query(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        query = _parse_query(d.pop("query", UNSET))


        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))


        fleet_open_transaction = cls(
            transaction_age_seconds=transaction_age_seconds,
            application_name=application_name,
            query=query,
            state=state,
        )

        return fleet_open_transaction
