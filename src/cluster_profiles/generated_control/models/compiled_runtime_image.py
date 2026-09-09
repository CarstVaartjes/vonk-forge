from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_runtime_image_source import check_compiled_runtime_image_source
from ..models.compiled_runtime_image_source import CompiledRuntimeImageSource
from typing import cast
from typing import cast, Union
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.compiled_distribution_object import CompiledDistributionObject





T = TypeVar("T", bound="CompiledRuntimeImage")



@_attrs_define
class CompiledRuntimeImage:
    """
        Attributes:
            architecture (Literal['linux-arm64']):
            build_id (Union[None, str]):
            distribution_object (CompiledDistributionObject): Distribution objects usable as installed model or runtime
                inputs.
            image_bytes (int):
            image_digest (str):
            local_image_config_id (str):
            local_image_reference (str):
            oci_layout_sha256 (str):
            platform_manifest_digest (str):
            registry_manifest_digest (Union[None, str]):
            runtime_interface (Literal['vonk.runtime.v1']):
            runtime_interface_label (str):
            source (CompiledRuntimeImageSource):
     """

    architecture: Literal['linux-arm64']
    build_id: Union[None, str]
    distribution_object: 'CompiledDistributionObject'
    image_bytes: int
    image_digest: str
    local_image_config_id: str
    local_image_reference: str
    oci_layout_sha256: str
    platform_manifest_digest: str
    registry_manifest_digest: Union[None, str]
    runtime_interface: Literal['vonk.runtime.v1']
    runtime_interface_label: str
    source: CompiledRuntimeImageSource





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_distribution_object import CompiledDistributionObject
        architecture = self.architecture

        build_id: Union[None, str]
        build_id = self.build_id

        distribution_object = self.distribution_object.to_dict()

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        local_image_config_id = self.local_image_config_id

        local_image_reference = self.local_image_reference

        oci_layout_sha256 = self.oci_layout_sha256

        platform_manifest_digest = self.platform_manifest_digest

        registry_manifest_digest: Union[None, str]
        registry_manifest_digest = self.registry_manifest_digest

        runtime_interface = self.runtime_interface

        runtime_interface_label = self.runtime_interface_label

        source: str = self.source


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "build_id": build_id,
            "distribution_object": distribution_object,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "local_image_config_id": local_image_config_id,
            "local_image_reference": local_image_reference,
            "oci_layout_sha256": oci_layout_sha256,
            "platform_manifest_digest": platform_manifest_digest,
            "registry_manifest_digest": registry_manifest_digest,
            "runtime_interface": runtime_interface,
            "runtime_interface_label": runtime_interface_label,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_distribution_object import CompiledDistributionObject
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        def _parse_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        build_id = _parse_build_id(d.pop("build_id"))


        distribution_object = CompiledDistributionObject.from_dict(d.pop("distribution_object"))




        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        local_image_config_id = d.pop("local_image_config_id")

        local_image_reference = d.pop("local_image_reference")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        platform_manifest_digest = d.pop("platform_manifest_digest")

        def _parse_registry_manifest_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest"))


        runtime_interface = cast(Literal['vonk.runtime.v1'] , d.pop("runtime_interface"))
        if runtime_interface != 'vonk.runtime.v1':
            raise ValueError(f"runtime_interface must match const 'vonk.runtime.v1', got '{runtime_interface}'")

        runtime_interface_label = d.pop("runtime_interface_label")

        source = check_compiled_runtime_image_source(d.pop("source"))




        compiled_runtime_image = cls(
            architecture=architecture,
            build_id=build_id,
            distribution_object=distribution_object,
            image_bytes=image_bytes,
            image_digest=image_digest,
            local_image_config_id=local_image_config_id,
            local_image_reference=local_image_reference,
            oci_layout_sha256=oci_layout_sha256,
            platform_manifest_digest=platform_manifest_digest,
            registry_manifest_digest=registry_manifest_digest,
            runtime_interface=runtime_interface,
            runtime_interface_label=runtime_interface_label,
            source=source,
        )

        return compiled_runtime_image
