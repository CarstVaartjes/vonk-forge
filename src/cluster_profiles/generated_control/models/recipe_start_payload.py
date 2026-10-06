from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_start_payload_phase_type_0 import check_recipe_start_payload_phase_type_0
from ..models.recipe_start_payload_phase_type_0 import RecipeStartPayloadPhaseType0
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_execution_plan import CompiledExecutionPlan





T = TypeVar("T", bound="RecipeStartPayload")



@_attrs_define
class RecipeStartPayload:
    """ Start one rank; placement, image and addresses come from the plan.

        Attributes:
            compiled_execution_plan (CompiledExecutionPlan):
            installation_id (str):
            mapping_id (str):
            plan_digest (str):
            recipe_revision_id (str):
            run_generation (int):
            run_id (str):
            phase (None | RecipeStartPayloadPhaseType0 | Unset):
            start_deadline (None | str | Unset):
     """

    compiled_execution_plan: CompiledExecutionPlan
    installation_id: str
    mapping_id: str
    plan_digest: str
    recipe_revision_id: str
    run_generation: int
    run_id: str
    phase: None | RecipeStartPayloadPhaseType0 | Unset = UNSET
    start_deadline: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        compiled_execution_plan = self.compiled_execution_plan.to_dict()

        installation_id = self.installation_id

        mapping_id = self.mapping_id

        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        run_generation = self.run_generation

        run_id = self.run_id

        phase: None | str | Unset
        if isinstance(self.phase, Unset):
            phase = UNSET
        elif isinstance(self.phase, str):
            phase = self.phase
        else:
            phase = self.phase

        start_deadline: None | str | Unset
        if isinstance(self.start_deadline, Unset):
            start_deadline = UNSET
        else:
            start_deadline = self.start_deadline


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_execution_plan": compiled_execution_plan,
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "run_generation": run_generation,
            "run_id": run_id,
        })
        if phase is not UNSET:
            field_dict["phase"] = phase
        if start_deadline is not UNSET:
            field_dict["start_deadline"] = start_deadline

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

        def _parse_phase(data: object) -> None | RecipeStartPayloadPhaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_recipe_start_payload_phase_type_0(data)



                return phase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeStartPayloadPhaseType0 | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_start_deadline(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        start_deadline = _parse_start_deadline(d.pop("start_deadline", UNSET))


        recipe_start_payload = cls(
            compiled_execution_plan=compiled_execution_plan,
            installation_id=installation_id,
            mapping_id=mapping_id,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            run_generation=run_generation,
            run_id=run_id,
            phase=phase,
            start_deadline=start_deadline,
        )

        return recipe_start_payload
