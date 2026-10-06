from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_start_payload import RecipeStartPayload





T = TypeVar("T", bound="RecoveryStartItem")



@_attrs_define
class RecoveryStartItem:
    """
        Attributes:
            node_id (str):
            payload (RecipeStartPayload): Start one rank; placement, image and addresses come from the plan.
     """

    node_id: str
    payload: RecipeStartPayload





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_start_payload import RecipeStartPayload # noqa: PLC0415
        node_id = self.node_id

        payload = self.payload.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "payload": payload,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_start_payload import RecipeStartPayload # noqa: PLC0415
        d = dict(src_dict)
        node_id = d.pop("node_id")

        payload = RecipeStartPayload.from_dict(d.pop("payload"))




        recovery_start_item = cls(
            node_id=node_id,
            payload=payload,
        )

        return recovery_start_item
