from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.visual_metadata import VisualMetadata
  from ..models.visual_provenance import VisualProvenance
  from ..models.visual_workload import VisualWorkload
  from ..models.visual_artifact import VisualArtifact
  from ..models.visual_build import VisualBuild
  from ..models.visual_identity import VisualIdentity
  from ..models.visual_validation import VisualValidation
  from ..models.visual_runtime import VisualRuntime





T = TypeVar("T", bound="VisualRecipeDocument")



@_attrs_define
class VisualRecipeDocument:
    """
        Attributes:
            artifacts (list['VisualArtifact']):
            build (VisualBuild):
            identity (VisualIdentity):
            metadata (VisualMetadata):
            provenance (VisualProvenance):
            runtime (VisualRuntime):
            schema_version (Literal[1]):
            validation (VisualValidation):
            workload (VisualWorkload):
     """

    artifacts: list['VisualArtifact']
    build: 'VisualBuild'
    identity: 'VisualIdentity'
    metadata: 'VisualMetadata'
    provenance: 'VisualProvenance'
    runtime: 'VisualRuntime'
    schema_version: Literal[1]
    validation: 'VisualValidation'
    workload: 'VisualWorkload'





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_metadata import VisualMetadata
        from ..models.visual_provenance import VisualProvenance
        from ..models.visual_workload import VisualWorkload
        from ..models.visual_artifact import VisualArtifact
        from ..models.visual_build import VisualBuild
        from ..models.visual_identity import VisualIdentity
        from ..models.visual_validation import VisualValidation
        from ..models.visual_runtime import VisualRuntime
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        build = self.build.to_dict()

        identity = self.identity.to_dict()

        metadata = self.metadata.to_dict()

        provenance = self.provenance.to_dict()

        runtime = self.runtime.to_dict()

        schema_version = self.schema_version

        validation = self.validation.to_dict()

        workload = self.workload.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "build": build,
            "identity": identity,
            "metadata": metadata,
            "provenance": provenance,
            "runtime": runtime,
            "schema_version": schema_version,
            "validation": validation,
            "workload": workload,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_metadata import VisualMetadata
        from ..models.visual_provenance import VisualProvenance
        from ..models.visual_workload import VisualWorkload
        from ..models.visual_artifact import VisualArtifact
        from ..models.visual_build import VisualBuild
        from ..models.visual_identity import VisualIdentity
        from ..models.visual_validation import VisualValidation
        from ..models.visual_runtime import VisualRuntime
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = VisualArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        build = VisualBuild.from_dict(d.pop("build"))




        identity = VisualIdentity.from_dict(d.pop("identity"))




        metadata = VisualMetadata.from_dict(d.pop("metadata"))




        provenance = VisualProvenance.from_dict(d.pop("provenance"))




        runtime = VisualRuntime.from_dict(d.pop("runtime"))




        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        validation = VisualValidation.from_dict(d.pop("validation"))




        workload = VisualWorkload.from_dict(d.pop("workload"))




        visual_recipe_document = cls(
            artifacts=artifacts,
            build=build,
            identity=identity,
            metadata=metadata,
            provenance=provenance,
            runtime=runtime,
            schema_version=schema_version,
            validation=validation,
            workload=workload,
        )

        return visual_recipe_document
