from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_run_effect import FleetProfileRunEffect





T = TypeVar("T", bound="FleetProfileAdoptedStopEffect")



@_attrs_define
class FleetProfileAdoptedStopEffect:
    """ Exact original cleanup, retained by a newer whole-fleet decision.

        Attributes:
            effect (FleetProfileRunEffect):
            operation_id (str):
            queue_index (int):
            request_key (str):
     """

    effect: FleetProfileRunEffect
    operation_id: str
    queue_index: int
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect # noqa: PLC0415
        effect = self.effect.to_dict()

        operation_id = self.operation_id

        queue_index = self.queue_index

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "effect": effect,
            "operation_id": operation_id,
            "queue_index": queue_index,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect # noqa: PLC0415
        d = dict(src_dict)
        effect = FleetProfileRunEffect.from_dict(d.pop("effect"))




        operation_id = d.pop("operation_id")

        queue_index = d.pop("queue_index")

        request_key = d.pop("request_key")

        fleet_profile_adopted_stop_effect = cls(
            effect=effect,
            operation_id=operation_id,
            queue_index=queue_index,
            request_key=request_key,
        )

        return fleet_profile_adopted_stop_effect
