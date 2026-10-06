from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_reconcile_payload import RecipeReconcilePayload





T = TypeVar("T", bound="ReconcilePhaseOperation")



@_attrs_define
class ReconcilePhaseOperation:
    """
        Attributes:
            node_id (str):
            operation_id (str):
            payload (RecipeReconcilePayload): Authority to remove one managed install with an invalid launch contract.
     """

    node_id: str
    operation_id: str
    payload: RecipeReconcilePayload





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_reconcile_payload import RecipeReconcilePayload # noqa: PLC0415
        node_id = self.node_id

        operation_id = self.operation_id

        payload = self.payload.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "operation_id": operation_id,
            "payload": payload,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_reconcile_payload import RecipeReconcilePayload # noqa: PLC0415
        d = dict(src_dict)
        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        payload = RecipeReconcilePayload.from_dict(d.pop("payload"))




        reconcile_phase_operation = cls(
            node_id=node_id,
            operation_id=operation_id,
            payload=payload,
        )

        return reconcile_phase_operation
