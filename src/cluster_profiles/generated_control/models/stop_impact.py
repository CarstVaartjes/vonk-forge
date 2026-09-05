from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="StopImpact")



@_attrs_define
class StopImpact:
    """
        Attributes:
            alias (str):
            node_ids (list[str]):
            plan_digest (str):
            reserved_bytes (int):
            run_id (str):
            state (str):
     """

    alias: str
    node_ids: list[str]
    plan_digest: str
    reserved_bytes: int
    run_id: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        alias = self.alias

        node_ids = self.node_ids



        plan_digest = self.plan_digest

        reserved_bytes = self.reserved_bytes

        run_id = self.run_id

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "reserved_bytes": reserved_bytes,
            "run_id": run_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alias = d.pop("alias")

        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        reserved_bytes = d.pop("reserved_bytes")

        run_id = d.pop("run_id")

        state = d.pop("state")

        stop_impact = cls(
            alias=alias,
            node_ids=node_ids,
            plan_digest=plan_digest,
            reserved_bytes=reserved_bytes,
            run_id=run_id,
            state=state,
        )

        return stop_impact
