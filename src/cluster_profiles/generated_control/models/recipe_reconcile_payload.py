from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeReconcilePayload")



@_attrs_define
class RecipeReconcilePayload:
    """ Authority to remove one managed install with an invalid launch contract.

        Attributes:
            installation_id (str):
            plan_digest (str):
     """

    installation_id: str
    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        plan_digest = d.pop("plan_digest")

        recipe_reconcile_payload = cls(
            installation_id=installation_id,
            plan_digest=plan_digest,
        )

        return recipe_reconcile_payload
