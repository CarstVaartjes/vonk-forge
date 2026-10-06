from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_artifact import CompiledArtifact
  from ..models.compiled_endpoint import CompiledEndpoint
  from ..models.compiled_identity import CompiledIdentity
  from ..models.compiled_job import CompiledJob
  from ..models.compiled_lifecycle import CompiledLifecycle
  from ..models.compiled_runtime import CompiledRuntime
  from ..models.compiled_runtime_image import CompiledRuntimeImage
  from ..models.compiled_security import CompiledSecurity
  from ..models.compiled_topology import CompiledTopology





T = TypeVar("T", bound="CompiledExecutionPlan")



@_attrs_define
class CompiledExecutionPlan:
    """
        Attributes:
            artifacts (list[CompiledArtifact]):
            endpoint (CompiledEndpoint | None):
            identity (CompiledIdentity):
            job (CompiledJob | None):
            lifecycle (CompiledLifecycle):
            runtime (CompiledRuntime):
            runtime_image (CompiledRuntimeImage): A runtime image in the Controller's layered store.

                ``image_digest`` is its manifest digest and ``oci_layout_sha256`` the same
                digest's hex, its address in the store; ``image_bytes`` is the size of its
                layers. Sparks pull it into Docker as
                ``localhost/vonk/compiled-runtime-<oci_layout_sha256>@<image_digest>``.
            security (CompiledSecurity): Per-workload security choices; everything else is a platform constant.
            topology (CompiledTopology):
     """

    artifacts: list[CompiledArtifact]
    endpoint: CompiledEndpoint | None
    identity: CompiledIdentity
    job: CompiledJob | None
    lifecycle: CompiledLifecycle
    runtime: CompiledRuntime
    runtime_image: CompiledRuntimeImage
    security: CompiledSecurity
    topology: CompiledTopology





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_artifact import CompiledArtifact # noqa: PLC0415
        from ..models.compiled_endpoint import CompiledEndpoint # noqa: PLC0415
        from ..models.compiled_identity import CompiledIdentity # noqa: PLC0415
        from ..models.compiled_job import CompiledJob # noqa: PLC0415
        from ..models.compiled_lifecycle import CompiledLifecycle # noqa: PLC0415
        from ..models.compiled_runtime import CompiledRuntime # noqa: PLC0415
        from ..models.compiled_runtime_image import CompiledRuntimeImage # noqa: PLC0415
        from ..models.compiled_security import CompiledSecurity # noqa: PLC0415
        from ..models.compiled_topology import CompiledTopology # noqa: PLC0415
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        endpoint: dict[str, Any] | None
        if isinstance(self.endpoint, CompiledEndpoint):
            endpoint = self.endpoint.to_dict()
        else:
            endpoint = self.endpoint

        identity = self.identity.to_dict()

        job: dict[str, Any] | None
        if isinstance(self.job, CompiledJob):
            job = self.job.to_dict()
        else:
            job = self.job

        lifecycle = self.lifecycle.to_dict()

        runtime = self.runtime.to_dict()

        runtime_image = self.runtime_image.to_dict()

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
            "security": security,
            "topology": topology,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_artifact import CompiledArtifact # noqa: PLC0415
        from ..models.compiled_endpoint import CompiledEndpoint # noqa: PLC0415
        from ..models.compiled_identity import CompiledIdentity # noqa: PLC0415
        from ..models.compiled_job import CompiledJob # noqa: PLC0415
        from ..models.compiled_lifecycle import CompiledLifecycle # noqa: PLC0415
        from ..models.compiled_runtime import CompiledRuntime # noqa: PLC0415
        from ..models.compiled_runtime_image import CompiledRuntimeImage # noqa: PLC0415
        from ..models.compiled_security import CompiledSecurity # noqa: PLC0415
        from ..models.compiled_topology import CompiledTopology # noqa: PLC0415
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = CompiledArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        def _parse_endpoint(data: object) -> CompiledEndpoint | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                endpoint_type_0 = CompiledEndpoint.from_dict(data)



                return endpoint_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledEndpoint | None, data)

        endpoint = _parse_endpoint(d.pop("endpoint"))


        identity = CompiledIdentity.from_dict(d.pop("identity"))




        def _parse_job(data: object) -> CompiledJob | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                job_type_0 = CompiledJob.from_dict(data)



                return job_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledJob | None, data)

        job = _parse_job(d.pop("job"))


        lifecycle = CompiledLifecycle.from_dict(d.pop("lifecycle"))




        runtime = CompiledRuntime.from_dict(d.pop("runtime"))




        runtime_image = CompiledRuntimeImage.from_dict(d.pop("runtime_image"))




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
            security=security,
            topology=topology,
        )

        return compiled_execution_plan
