from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_lock_holder import FleetLockHolder
  from ..models.fleet_open_transaction import FleetOpenTransaction





T = TypeVar("T", bound="FleetLocksResponse")



@_attrs_define
class FleetLocksResponse:
    """
        Attributes:
            held (list[FleetLockHolder]):
            open_transactions (list[FleetOpenTransaction]):
     """

    held: list[FleetLockHolder]
    open_transactions: list[FleetOpenTransaction]





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_lock_holder import FleetLockHolder # noqa: PLC0415
        from ..models.fleet_open_transaction import FleetOpenTransaction # noqa: PLC0415
        held = []
        for held_item_data in self.held:
            held_item = held_item_data.to_dict()
            held.append(held_item)



        open_transactions = []
        for open_transactions_item_data in self.open_transactions:
            open_transactions_item = open_transactions_item_data.to_dict()
            open_transactions.append(open_transactions_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "held": held,
            "open_transactions": open_transactions,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_lock_holder import FleetLockHolder # noqa: PLC0415
        from ..models.fleet_open_transaction import FleetOpenTransaction # noqa: PLC0415
        d = dict(src_dict)
        held = []
        _held = d.pop("held")
        for held_item_data in (_held):
            held_item = FleetLockHolder.from_dict(held_item_data)



            held.append(held_item)


        open_transactions = []
        _open_transactions = d.pop("open_transactions")
        for open_transactions_item_data in (_open_transactions):
            open_transactions_item = FleetOpenTransaction.from_dict(open_transactions_item_data)



            open_transactions.append(open_transactions_item)


        fleet_locks_response = cls(
            held=held,
            open_transactions=open_transactions,
        )

        return fleet_locks_response
