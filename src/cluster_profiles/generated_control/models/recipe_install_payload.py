from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_execution_plan import CompiledExecutionPlan





T = TypeVar("T", bound="RecipeInstallPayload")



@_attrs_define
class RecipeInstallPayload:
    """
        Attributes:
            compiled_execution_plan (CompiledExecutionPlan):
            expected_bytes (int):
            installation_id (str):
            plan_digest (str):
     """

    compiled_execution_plan: CompiledExecutionPlan
    expected_bytes: int
    installation_id: str
    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        compiled_execution_plan = self.compiled_execution_plan.to_dict()

        expected_bytes = self.expected_bytes

        installation_id = self.installation_id

        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_execution_plan": compiled_execution_plan,
            "expected_bytes": expected_bytes,
            "installation_id": installation_id,
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        d = dict(src_dict)
        compiled_execution_plan = CompiledExecutionPlan.from_dict(d.pop("compiled_execution_plan"))




        expected_bytes = d.pop("expected_bytes")

        installation_id = d.pop("installation_id")

        plan_digest = d.pop("plan_digest")

        recipe_install_payload = cls(
            compiled_execution_plan=compiled_execution_plan,
            expected_bytes=expected_bytes,
            installation_id=installation_id,
            plan_digest=plan_digest,
        )

        return recipe_install_payload
