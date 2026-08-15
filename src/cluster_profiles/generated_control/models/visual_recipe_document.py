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
  from ..models.visual_catalog_identity import VisualCatalogIdentity
  from ..models.visual_execution import VisualExecution
  from ..models.visual_build import VisualBuild
  from ..models.visual_artifact import VisualArtifact
  from ..models.visual_identity import VisualIdentity
  from ..models.visual_validation import VisualValidation
  from ..models.visual_runtime import VisualRuntime
  from ..models.visual_interface import VisualInterface





T = TypeVar("T", bound="VisualRecipeDocument")



@_attrs_define
class VisualRecipeDocument:
    """
        Attributes:
            artifacts (list['VisualArtifact']):
            build (VisualBuild):
            execution (VisualExecution):
            identity (VisualIdentity):
            interfaces (list['VisualInterface']):
            metadata (VisualMetadata):
            model (VisualCatalogIdentity):
            provenance (VisualProvenance):
            runtime (VisualRuntime):
            schema_version (Literal[1]):
            validation (VisualValidation):
     """

    artifacts: list['VisualArtifact']
    build: 'VisualBuild'
    execution: 'VisualExecution'
    identity: 'VisualIdentity'
    interfaces: list['VisualInterface']
    metadata: 'VisualMetadata'
    model: 'VisualCatalogIdentity'
    provenance: 'VisualProvenance'
    runtime: 'VisualRuntime'
    schema_version: Literal[1]
    validation: 'VisualValidation'





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_metadata import VisualMetadata
        from ..models.visual_provenance import VisualProvenance
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        from ..models.visual_execution import VisualExecution
        from ..models.visual_build import VisualBuild
        from ..models.visual_artifact import VisualArtifact
        from ..models.visual_identity import VisualIdentity
        from ..models.visual_validation import VisualValidation
        from ..models.visual_runtime import VisualRuntime
        from ..models.visual_interface import VisualInterface
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        build = self.build.to_dict()

        execution = self.execution.to_dict()

        identity = self.identity.to_dict()

        interfaces = []
        for interfaces_item_data in self.interfaces:
            interfaces_item = interfaces_item_data.to_dict()
            interfaces.append(interfaces_item)



        metadata = self.metadata.to_dict()

        model = self.model.to_dict()

        provenance = self.provenance.to_dict()

        runtime = self.runtime.to_dict()

        schema_version = self.schema_version

        validation = self.validation.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "build": build,
            "execution": execution,
            "identity": identity,
            "interfaces": interfaces,
            "metadata": metadata,
            "model": model,
            "provenance": provenance,
            "runtime": runtime,
            "schema_version": schema_version,
            "validation": validation,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_metadata import VisualMetadata
        from ..models.visual_provenance import VisualProvenance
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        from ..models.visual_execution import VisualExecution
        from ..models.visual_build import VisualBuild
        from ..models.visual_artifact import VisualArtifact
        from ..models.visual_identity import VisualIdentity
        from ..models.visual_validation import VisualValidation
        from ..models.visual_runtime import VisualRuntime
        from ..models.visual_interface import VisualInterface
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = VisualArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        build = VisualBuild.from_dict(d.pop("build"))




        execution = VisualExecution.from_dict(d.pop("execution"))




        identity = VisualIdentity.from_dict(d.pop("identity"))




        interfaces = []
        _interfaces = d.pop("interfaces")
        for interfaces_item_data in (_interfaces):
            interfaces_item = VisualInterface.from_dict(interfaces_item_data)



            interfaces.append(interfaces_item)


        metadata = VisualMetadata.from_dict(d.pop("metadata"))




        model = VisualCatalogIdentity.from_dict(d.pop("model"))




        provenance = VisualProvenance.from_dict(d.pop("provenance"))




        runtime = VisualRuntime.from_dict(d.pop("runtime"))




        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        validation = VisualValidation.from_dict(d.pop("validation"))




        visual_recipe_document = cls(
            artifacts=artifacts,
            build=build,
            execution=execution,
            identity=identity,
            interfaces=interfaces,
            metadata=metadata,
            model=model,
            provenance=provenance,
            runtime=runtime,
            schema_version=schema_version,
            validation=validation,
        )

        return visual_recipe_document
