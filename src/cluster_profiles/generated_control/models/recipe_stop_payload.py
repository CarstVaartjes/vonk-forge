from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_execution_plan import CompiledExecutionPlan





T = TypeVar("T", bound="RecipeStopPayload")



@_attrs_define
class RecipeStopPayload:
    """
        Attributes:
            compiled_execution_plan (CompiledExecutionPlan):
            installation_id (str):
            mapping_id (str):
            plan_digest (str):
            recipe_revision_id (str):
            run_generation (int):
            run_id (str):
            target_runtime_id (str):
            cancel_pending_start (bool | Unset):  Default: False.
     """

    compiled_execution_plan: CompiledExecutionPlan
    installation_id: str
    mapping_id: str
    plan_digest: str
    recipe_revision_id: str
    run_generation: int
    run_id: str
    target_runtime_id: str
    cancel_pending_start: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        compiled_execution_plan = self.compiled_execution_plan.to_dict()

        installation_id = self.installation_id

        mapping_id = self.mapping_id

        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        run_generation = self.run_generation

        run_id = self.run_id

        target_runtime_id = self.target_runtime_id

        cancel_pending_start = self.cancel_pending_start


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_execution_plan": compiled_execution_plan,
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "run_generation": run_generation,
            "run_id": run_id,
            "target_runtime_id": target_runtime_id,
        })
        if cancel_pending_start is not UNSET:
            field_dict["cancel_pending_start"] = cancel_pending_start

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        d = dict(src_dict)
        compiled_execution_plan = CompiledExecutionPlan.from_dict(d.pop("compiled_execution_plan"))




        installation_id = d.pop("installation_id")

        mapping_id = d.pop("mapping_id")

        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        run_generation = d.pop("run_generation")

        run_id = d.pop("run_id")

        target_runtime_id = d.pop("target_runtime_id")

        cancel_pending_start = d.pop("cancel_pending_start", UNSET)

        recipe_stop_payload = cls(
            compiled_execution_plan=compiled_execution_plan,
            installation_id=installation_id,
            mapping_id=mapping_id,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            run_generation=run_generation,
            run_id=run_id,
            target_runtime_id=target_runtime_id,
            cancel_pending_start=cancel_pending_start,
        )

        return recipe_stop_payload
