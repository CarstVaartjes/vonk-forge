from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_execution_plan import CompiledExecutionPlan
  from ..models.recipe_job_input_file import RecipeJobInputFile
  from ..models.recipe_job_output_limits import RecipeJobOutputLimits
  from ..models.recipe_job_output_mapping import RecipeJobOutputMapping





T = TypeVar("T", bound="RecipeJobRunRequest")



@_attrs_define
class RecipeJobRunRequest:
    """ One job run; interface, image, placement and timeout come from the plan.

        Attributes:
            compiled_execution_plan (CompiledExecutionPlan):
            input_manifest_sha256 (str):
            input_total_bytes (int):
            inputs (list[RecipeJobInputFile]):
            installation_id (str):
            job_id (str):
            mapping_id (str):
            output_limits (RecipeJobOutputLimits):
            output_mappings (list[RecipeJobOutputMapping]):
            plan_digest (str):
            recipe_revision_id (str):
            run_generation (int):
            run_id (str):
     """

    compiled_execution_plan: CompiledExecutionPlan
    input_manifest_sha256: str
    input_total_bytes: int
    inputs: list[RecipeJobInputFile]
    installation_id: str
    job_id: str
    mapping_id: str
    output_limits: RecipeJobOutputLimits
    output_mappings: list[RecipeJobOutputMapping]
    plan_digest: str
    recipe_revision_id: str
    run_generation: int
    run_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        from ..models.recipe_job_input_file import RecipeJobInputFile # noqa: PLC0415
        from ..models.recipe_job_output_limits import RecipeJobOutputLimits # noqa: PLC0415
        from ..models.recipe_job_output_mapping import RecipeJobOutputMapping # noqa: PLC0415
        compiled_execution_plan = self.compiled_execution_plan.to_dict()

        input_manifest_sha256 = self.input_manifest_sha256

        input_total_bytes = self.input_total_bytes

        inputs = []
        for inputs_item_data in self.inputs:
            inputs_item = inputs_item_data.to_dict()
            inputs.append(inputs_item)



        installation_id = self.installation_id

        job_id = self.job_id

        mapping_id = self.mapping_id

        output_limits = self.output_limits.to_dict()

        output_mappings = []
        for output_mappings_item_data in self.output_mappings:
            output_mappings_item = output_mappings_item_data.to_dict()
            output_mappings.append(output_mappings_item)



        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        run_generation = self.run_generation

        run_id = self.run_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_execution_plan": compiled_execution_plan,
            "input_manifest_sha256": input_manifest_sha256,
            "input_total_bytes": input_total_bytes,
            "inputs": inputs,
            "installation_id": installation_id,
            "job_id": job_id,
            "mapping_id": mapping_id,
            "output_limits": output_limits,
            "output_mappings": output_mappings,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "run_generation": run_generation,
            "run_id": run_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_execution_plan import CompiledExecutionPlan # noqa: PLC0415
        from ..models.recipe_job_input_file import RecipeJobInputFile # noqa: PLC0415
        from ..models.recipe_job_output_limits import RecipeJobOutputLimits # noqa: PLC0415
        from ..models.recipe_job_output_mapping import RecipeJobOutputMapping # noqa: PLC0415
        d = dict(src_dict)
        compiled_execution_plan = CompiledExecutionPlan.from_dict(d.pop("compiled_execution_plan"))




        input_manifest_sha256 = d.pop("input_manifest_sha256")

        input_total_bytes = d.pop("input_total_bytes")

        inputs = []
        _inputs = d.pop("inputs")
        for inputs_item_data in (_inputs):
            inputs_item = RecipeJobInputFile.from_dict(inputs_item_data)



            inputs.append(inputs_item)


        installation_id = d.pop("installation_id")

        job_id = d.pop("job_id")

        mapping_id = d.pop("mapping_id")

        output_limits = RecipeJobOutputLimits.from_dict(d.pop("output_limits"))




        output_mappings = []
        _output_mappings = d.pop("output_mappings")
        for output_mappings_item_data in (_output_mappings):
            output_mappings_item = RecipeJobOutputMapping.from_dict(output_mappings_item_data)



            output_mappings.append(output_mappings_item)


        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        run_generation = d.pop("run_generation")

        run_id = d.pop("run_id")

        recipe_job_run_request = cls(
            compiled_execution_plan=compiled_execution_plan,
            input_manifest_sha256=input_manifest_sha256,
            input_total_bytes=input_total_bytes,
            inputs=inputs,
            installation_id=installation_id,
            job_id=job_id,
            mapping_id=mapping_id,
            output_limits=output_limits,
            output_mappings=output_mappings,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            run_generation=run_generation,
            run_id=run_id,
        )

        return recipe_job_run_request
