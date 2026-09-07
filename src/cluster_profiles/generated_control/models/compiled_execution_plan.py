from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.compiled_runtime_image import CompiledRuntimeImage
  from ..models.compiled_model_artifact import CompiledModelArtifact





T = TypeVar("T", bound="CompiledExecutionPlan")



@_attrs_define
class CompiledExecutionPlan:
    """ Internal verified execution plan consumed by distribution/install.

        Attributes:
            artifacts (list['CompiledModelArtifact']):
            execution_sha256 (str):
            harness_sha256 (str):
            model_artifact_set_bytes (int):
            model_artifact_set_sha256 (str):
            recipe_revision_sha256 (str):
            runtime_image (CompiledRuntimeImage): The exact OCI archive that the Controller gives to each Spark.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    artifacts: list['CompiledModelArtifact']
    execution_sha256: str
    harness_sha256: str
    model_artifact_set_bytes: int
    model_artifact_set_sha256: str
    recipe_revision_sha256: str
    runtime_image: 'CompiledRuntimeImage'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_runtime_image import CompiledRuntimeImage
        from ..models.compiled_model_artifact import CompiledModelArtifact
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        execution_sha256 = self.execution_sha256

        harness_sha256 = self.harness_sha256

        model_artifact_set_bytes = self.model_artifact_set_bytes

        model_artifact_set_sha256 = self.model_artifact_set_sha256

        recipe_revision_sha256 = self.recipe_revision_sha256

        runtime_image = self.runtime_image.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "execution_sha256": execution_sha256,
            "harness_sha256": harness_sha256,
            "model_artifact_set_bytes": model_artifact_set_bytes,
            "model_artifact_set_sha256": model_artifact_set_sha256,
            "recipe_revision_sha256": recipe_revision_sha256,
            "runtime_image": runtime_image,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_runtime_image import CompiledRuntimeImage
        from ..models.compiled_model_artifact import CompiledModelArtifact
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = CompiledModelArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        execution_sha256 = d.pop("execution_sha256")

        harness_sha256 = d.pop("harness_sha256")

        model_artifact_set_bytes = d.pop("model_artifact_set_bytes")

        model_artifact_set_sha256 = d.pop("model_artifact_set_sha256")

        recipe_revision_sha256 = d.pop("recipe_revision_sha256")

        runtime_image = CompiledRuntimeImage.from_dict(d.pop("runtime_image"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        compiled_execution_plan = cls(
            artifacts=artifacts,
            execution_sha256=execution_sha256,
            harness_sha256=harness_sha256,
            model_artifact_set_bytes=model_artifact_set_bytes,
            model_artifact_set_sha256=model_artifact_set_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            runtime_image=runtime_image,
            schema_version=schema_version,
        )

        return compiled_execution_plan
