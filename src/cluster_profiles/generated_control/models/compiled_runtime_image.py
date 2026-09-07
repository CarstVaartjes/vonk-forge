from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_runtime_image_source import check_compiled_runtime_image_source
from ..models.compiled_runtime_image_source import CompiledRuntimeImageSource
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.distribution_object_receipt import DistributionObjectReceipt





T = TypeVar("T", bound="CompiledRuntimeImage")



@_attrs_define
class CompiledRuntimeImage:
    """ The exact OCI archive that the Controller gives to each Spark.

        Attributes:
            architecture (Literal['linux-arm64']):
            distribution_object (DistributionObjectReceipt): A verified immutable object served by the Controller.
            image_bytes (int):
            image_digest (str):
            local_image_config_id (str):
            oci_layout_sha256 (str):
            platform_manifest_digest (str):
            runtime_interface (str):
            runtime_interface_label (str):
            source (CompiledRuntimeImageSource):
            build_id (Union[None, Unset, str]):
            local_image_reference (Union[None, Unset, str]):
            registry_manifest_digest (Union[None, Unset, str]):
     """

    architecture: Literal['linux-arm64']
    distribution_object: 'DistributionObjectReceipt'
    image_bytes: int
    image_digest: str
    local_image_config_id: str
    oci_layout_sha256: str
    platform_manifest_digest: str
    runtime_interface: str
    runtime_interface_label: str
    source: CompiledRuntimeImageSource
    build_id: Union[None, Unset, str] = UNSET
    local_image_reference: Union[None, Unset, str] = UNSET
    registry_manifest_digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.distribution_object_receipt import DistributionObjectReceipt
        architecture = self.architecture

        distribution_object = self.distribution_object.to_dict()

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        local_image_config_id = self.local_image_config_id

        oci_layout_sha256 = self.oci_layout_sha256

        platform_manifest_digest = self.platform_manifest_digest

        runtime_interface = self.runtime_interface

        runtime_interface_label = self.runtime_interface_label

        source: str = self.source

        build_id: Union[None, Unset, str]
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        local_image_reference: Union[None, Unset, str]
        if isinstance(self.local_image_reference, Unset):
            local_image_reference = UNSET
        else:
            local_image_reference = self.local_image_reference

        registry_manifest_digest: Union[None, Unset, str]
        if isinstance(self.registry_manifest_digest, Unset):
            registry_manifest_digest = UNSET
        else:
            registry_manifest_digest = self.registry_manifest_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "distribution_object": distribution_object,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "local_image_config_id": local_image_config_id,
            "oci_layout_sha256": oci_layout_sha256,
            "platform_manifest_digest": platform_manifest_digest,
            "runtime_interface": runtime_interface,
            "runtime_interface_label": runtime_interface_label,
            "source": source,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if local_image_reference is not UNSET:
            field_dict["local_image_reference"] = local_image_reference
        if registry_manifest_digest is not UNSET:
            field_dict["registry_manifest_digest"] = registry_manifest_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.distribution_object_receipt import DistributionObjectReceipt
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        distribution_object = DistributionObjectReceipt.from_dict(d.pop("distribution_object"))




        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        local_image_config_id = d.pop("local_image_config_id")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        platform_manifest_digest = d.pop("platform_manifest_digest")

        runtime_interface = d.pop("runtime_interface")

        runtime_interface_label = d.pop("runtime_interface_label")

        source = check_compiled_runtime_image_source(d.pop("source"))




        def _parse_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_local_image_reference(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        local_image_reference = _parse_local_image_reference(d.pop("local_image_reference", UNSET))


        def _parse_registry_manifest_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest", UNSET))


        compiled_runtime_image = cls(
            architecture=architecture,
            distribution_object=distribution_object,
            image_bytes=image_bytes,
            image_digest=image_digest,
            local_image_config_id=local_image_config_id,
            oci_layout_sha256=oci_layout_sha256,
            platform_manifest_digest=platform_manifest_digest,
            runtime_interface=runtime_interface,
            runtime_interface_label=runtime_interface_label,
            source=source,
            build_id=build_id,
            local_image_reference=local_image_reference,
            registry_manifest_digest=registry_manifest_digest,
        )

        return compiled_runtime_image
