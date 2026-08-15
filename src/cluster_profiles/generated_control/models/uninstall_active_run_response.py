from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="UninstallActiveRunResponse")



@_attrs_define
class UninstallActiveRunResponse:
    """
        Attributes:
            alias (str):
            route_state (str):
            run_id (str):
            state (str):
     """

    alias: str
    route_state: str
    run_id: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        alias = self.alias

        route_state = self.route_state

        run_id = self.run_id

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "route_state": route_state,
            "run_id": run_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alias = d.pop("alias")

        route_state = d.pop("route_state")

        run_id = d.pop("run_id")

        state = d.pop("state")

        uninstall_active_run_response = cls(
            alias=alias,
            route_state=route_state,
            run_id=run_id,
            state=state,
        )

        return uninstall_active_run_response
