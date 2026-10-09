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





T = TypeVar("T", bound="RecipeUninstallPayload")



@_attrs_define
class RecipeUninstallPayload:
    """
        Attributes:
            cleanup_model_content_sha256 (None | str):
            installation_id (str):
            plan_digest (str):
            recipe_content_sha256 (str):
            compiled_execution_plan (CompiledExecutionPlan | None | Unset):
     """

    cleanup_model_content_sha256: None | str
    installation_id: str
    plan_digest: str
    recipe_content_sha256: str
    compiled_execution_plan: CompiledExecutionPlan | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        cleanup_model_content_sha256: None | str
        cleanup_model_content_sha256 = self.cleanup_model_content_sha256

        installation_id = self.installation_id

        plan_digest = self.plan_digest

        recipe_content_sha256 = self.recipe_content_sha256

        compiled_execution_plan: dict[str, Any] | None | Unset
        if isinstance(self.compiled_execution_plan, Unset):
            compiled_execution_plan = UNSET
        elif isinstance(self.compiled_execution_plan, CompiledExecutionPlan):
            compiled_execution_plan = self.compiled_execution_plan.to_dict()
        else:
            compiled_execution_plan = self.compiled_execution_plan


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cleanup_model_content_sha256": cleanup_model_content_sha256,
            "installation_id": installation_id,
            "plan_digest": plan_digest,
            "recipe_content_sha256": recipe_content_sha256,
        })
        if compiled_execution_plan is not UNSET:
            field_dict["compiled_execution_plan"] = compiled_execution_plan

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        d = dict(src_dict)
        def _parse_cleanup_model_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        cleanup_model_content_sha256 = _parse_cleanup_model_content_sha256(d.pop("cleanup_model_content_sha256"))


        installation_id = d.pop("installation_id")

        plan_digest = d.pop("plan_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        def _parse_compiled_execution_plan(data: object) -> CompiledExecutionPlan | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                compiled_execution_plan_type_0 = CompiledExecutionPlan.from_dict(data)



                return compiled_execution_plan_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledExecutionPlan | None | Unset, data)

        compiled_execution_plan = _parse_compiled_execution_plan(d.pop("compiled_execution_plan", UNSET))


        recipe_uninstall_payload = cls(
            cleanup_model_content_sha256=cleanup_model_content_sha256,
            installation_id=installation_id,
            plan_digest=plan_digest,
            recipe_content_sha256=recipe_content_sha256,
            compiled_execution_plan=compiled_execution_plan,
        )

        return recipe_uninstall_payload
