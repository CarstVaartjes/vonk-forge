from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetLockHolder")



@_attrs_define
class FleetLockHolder:
    """
        Attributes:
            holder (str):
            namespace (str):
            transaction_age_seconds (float):
            node_id (None | str | Unset):
            query (None | str | Unset):
            state (None | str | Unset):
     """

    holder: str
    namespace: str
    transaction_age_seconds: float
    node_id: None | str | Unset = UNSET
    query: None | str | Unset = UNSET
    state: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        holder = self.holder

        namespace = self.namespace

        transaction_age_seconds = self.transaction_age_seconds

        node_id: None | str | Unset
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

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
            "holder": holder,
            "namespace": namespace,
            "transaction_age_seconds": transaction_age_seconds,
        })
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if query is not UNSET:
            field_dict["query"] = query
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        holder = d.pop("holder")

        namespace = d.pop("namespace")

        transaction_age_seconds = d.pop("transaction_age_seconds")

        def _parse_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


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


        fleet_lock_holder = cls(
            holder=holder,
            namespace=namespace,
            transaction_age_seconds=transaction_age_seconds,
            node_id=node_id,
            query=query,
            state=state,
        )

        return fleet_lock_holder
