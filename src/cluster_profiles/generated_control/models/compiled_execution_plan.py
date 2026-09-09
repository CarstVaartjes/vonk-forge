from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.compiled_artifact import CompiledArtifact
  from ..models.compiled_topology import CompiledTopology
  from ..models.compiled_security import CompiledSecurity
  from ..models.compiled_lifecycle import CompiledLifecycle
  from ..models.compiled_runtime_image import CompiledRuntimeImage
  from ..models.compiled_runtime import CompiledRuntime
  from ..models.compiled_endpoint import CompiledEndpoint
  from ..models.compiled_identity import CompiledIdentity
  from ..models.compiled_job import CompiledJob





T = TypeVar("T", bound="CompiledExecutionPlan")



@_attrs_define
class CompiledExecutionPlan:
    """
        Attributes:
            artifacts (list['CompiledArtifact']):
            endpoint (Union['CompiledEndpoint', None]):
            identity (CompiledIdentity):
            job (Union['CompiledJob', None]):
            lifecycle (CompiledLifecycle):
            runtime (CompiledRuntime):
            runtime_image (CompiledRuntimeImage):
            schema_version (Literal[2]):
            security (CompiledSecurity):
            topology (CompiledTopology):
     """

    artifacts: list['CompiledArtifact']
    endpoint: Union['CompiledEndpoint', None]
    identity: 'CompiledIdentity'
    job: Union['CompiledJob', None]
    lifecycle: 'CompiledLifecycle'
    runtime: 'CompiledRuntime'
    runtime_image: 'CompiledRuntimeImage'
    schema_version: Literal[2]
    security: 'CompiledSecurity'
    topology: 'CompiledTopology'





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_artifact import CompiledArtifact
        from ..models.compiled_topology import CompiledTopology
        from ..models.compiled_security import CompiledSecurity
        from ..models.compiled_lifecycle import CompiledLifecycle
        from ..models.compiled_runtime_image import CompiledRuntimeImage
        from ..models.compiled_runtime import CompiledRuntime
        from ..models.compiled_endpoint import CompiledEndpoint
        from ..models.compiled_identity import CompiledIdentity
        from ..models.compiled_job import CompiledJob
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        endpoint: Union[None, dict[str, Any]]
        if isinstance(self.endpoint, CompiledEndpoint):
            endpoint = self.endpoint.to_dict()
        else:
            endpoint = self.endpoint

        identity = self.identity.to_dict()

        job: Union[None, dict[str, Any]]
        if isinstance(self.job, CompiledJob):
            job = self.job.to_dict()
        else:
            job = self.job

        lifecycle = self.lifecycle.to_dict()

        runtime = self.runtime.to_dict()

        runtime_image = self.runtime_image.to_dict()

        schema_version = self.schema_version

        security = self.security.to_dict()

        topology = self.topology.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "endpoint": endpoint,
            "identity": identity,
            "job": job,
            "lifecycle": lifecycle,
            "runtime": runtime,
            "runtime_image": runtime_image,
            "schema_version": schema_version,
            "security": security,
            "topology": topology,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_artifact import CompiledArtifact
        from ..models.compiled_topology import CompiledTopology
        from ..models.compiled_security import CompiledSecurity
        from ..models.compiled_lifecycle import CompiledLifecycle
        from ..models.compiled_runtime_image import CompiledRuntimeImage
        from ..models.compiled_runtime import CompiledRuntime
        from ..models.compiled_endpoint import CompiledEndpoint
        from ..models.compiled_identity import CompiledIdentity
        from ..models.compiled_job import CompiledJob
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = CompiledArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        def _parse_endpoint(data: object) -> Union['CompiledEndpoint', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                endpoint_type_0 = CompiledEndpoint.from_dict(data)



                return endpoint_type_0
            except: # noqa: E722
                pass
            return cast(Union['CompiledEndpoint', None], data)

        endpoint = _parse_endpoint(d.pop("endpoint"))


        identity = CompiledIdentity.from_dict(d.pop("identity"))




        def _parse_job(data: object) -> Union['CompiledJob', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                job_type_0 = CompiledJob.from_dict(data)



                return job_type_0
            except: # noqa: E722
                pass
            return cast(Union['CompiledJob', None], data)

        job = _parse_job(d.pop("job"))


        lifecycle = CompiledLifecycle.from_dict(d.pop("lifecycle"))




        runtime = CompiledRuntime.from_dict(d.pop("runtime"))




        runtime_image = CompiledRuntimeImage.from_dict(d.pop("runtime_image"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        security = CompiledSecurity.from_dict(d.pop("security"))




        topology = CompiledTopology.from_dict(d.pop("topology"))




        compiled_execution_plan = cls(
            artifacts=artifacts,
            endpoint=endpoint,
            identity=identity,
            job=job,
            lifecycle=lifecycle,
            runtime=runtime,
            runtime_image=runtime_image,
            schema_version=schema_version,
            security=security,
            topology=topology,
        )

        return compiled_execution_plan
