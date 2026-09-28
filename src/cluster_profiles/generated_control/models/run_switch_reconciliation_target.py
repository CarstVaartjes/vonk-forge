from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_reconciliation_target_state import check_run_switch_reconciliation_target_state
from ..models.run_switch_reconciliation_target_state import RunSwitchReconciliationTargetState
from typing import cast






T = TypeVar("T", bound="RunSwitchReconciliationTarget")



@_attrs_define
class RunSwitchReconciliationTarget:
    """ One exact rank and whether its cleanup already succeeded.

        Attributes:
            installed_bytes (int):
            node_id (str):
            rank (int):
            role (str):
            state (RunSwitchReconciliationTargetState):
     """

    installed_bytes: int
    node_id: str
    rank: int
    role: str
    state: RunSwitchReconciliationTargetState





    def to_dict(self) -> dict[str, Any]:
        installed_bytes = self.installed_bytes

        node_id = self.node_id

        rank = self.rank

        role = self.role

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installed_bytes": installed_bytes,
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installed_bytes = d.pop("installed_bytes")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        state = check_run_switch_reconciliation_target_state(d.pop("state"))




        run_switch_reconciliation_target = cls(
            installed_bytes=installed_bytes,
            node_id=node_id,
            rank=rank,
            role=role,
            state=state,
        )

        return run_switch_reconciliation_target
