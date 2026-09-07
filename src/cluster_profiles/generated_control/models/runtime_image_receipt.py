from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.runtime_image_receipt_source import check_runtime_image_receipt_source
from ..models.runtime_image_receipt_source import RuntimeImageReceiptSource
from typing import cast
from typing import cast, Union
from typing import Literal, cast






T = TypeVar("T", bound="RuntimeImageReceipt")



@_attrs_define
class RuntimeImageReceipt:
    """ Strict schema-2 receipt persisted by the Controller image cache.

    ``oci_archive_sha256`` is the established filesystem/SQL receipt field.
    The compiled launch plan uses its own ``oci_layout_sha256`` field; the
    execution-plan service performs that one explicit typed projection at the
    plan boundary.

        Attributes:
            architecture (Literal['linux-arm64']):
            archive_path (str):
            build_id (Union[None, str]):
            distribution_content_sha256 (str):
            distribution_publisher (str):
            distribution_slug (str):
            image_bytes (int):
            image_digest (str):
            local_image_config_id (Union[None, str]):
            local_image_reference (Union[None, str]):
            oci_archive_sha256 (str):
            platform_manifest_digest (str):
            recorded_at (str):
            registry_manifest_digest (Union[None, str]):
            runtime_interface (Literal['vonk.runtime.v1']):
            runtime_interface_label (Literal['v1']):
            schema_version (Literal[2]):
            source (RuntimeImageReceiptSource):
     """

    architecture: Literal['linux-arm64']
    archive_path: str
    build_id: Union[None, str]
    distribution_content_sha256: str
    distribution_publisher: str
    distribution_slug: str
    image_bytes: int
    image_digest: str
    local_image_config_id: Union[None, str]
    local_image_reference: Union[None, str]
    oci_archive_sha256: str
    platform_manifest_digest: str
    recorded_at: str
    registry_manifest_digest: Union[None, str]
    runtime_interface: Literal['vonk.runtime.v1']
    runtime_interface_label: Literal['v1']
    schema_version: Literal[2]
    source: RuntimeImageReceiptSource





    def to_dict(self) -> dict[str, Any]:
        architecture = self.architecture

        archive_path = self.archive_path

        build_id: Union[None, str]
        build_id = self.build_id

        distribution_content_sha256 = self.distribution_content_sha256

        distribution_publisher = self.distribution_publisher

        distribution_slug = self.distribution_slug

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        local_image_config_id: Union[None, str]
        local_image_config_id = self.local_image_config_id

        local_image_reference: Union[None, str]
        local_image_reference = self.local_image_reference

        oci_archive_sha256 = self.oci_archive_sha256

        platform_manifest_digest = self.platform_manifest_digest

        recorded_at = self.recorded_at

        registry_manifest_digest: Union[None, str]
        registry_manifest_digest = self.registry_manifest_digest

        runtime_interface = self.runtime_interface

        runtime_interface_label = self.runtime_interface_label

        schema_version = self.schema_version

        source: str = self.source


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "archive_path": archive_path,
            "build_id": build_id,
            "distribution_content_sha256": distribution_content_sha256,
            "distribution_publisher": distribution_publisher,
            "distribution_slug": distribution_slug,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "local_image_config_id": local_image_config_id,
            "local_image_reference": local_image_reference,
            "oci_archive_sha256": oci_archive_sha256,
            "platform_manifest_digest": platform_manifest_digest,
            "recorded_at": recorded_at,
            "registry_manifest_digest": registry_manifest_digest,
            "runtime_interface": runtime_interface,
            "runtime_interface_label": runtime_interface_label,
            "schema_version": schema_version,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        archive_path = d.pop("archive_path")

        def _parse_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        build_id = _parse_build_id(d.pop("build_id"))


        distribution_content_sha256 = d.pop("distribution_content_sha256")

        distribution_publisher = d.pop("distribution_publisher")

        distribution_slug = d.pop("distribution_slug")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        def _parse_local_image_config_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        local_image_config_id = _parse_local_image_config_id(d.pop("local_image_config_id"))


        def _parse_local_image_reference(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        local_image_reference = _parse_local_image_reference(d.pop("local_image_reference"))


        oci_archive_sha256 = d.pop("oci_archive_sha256")

        platform_manifest_digest = d.pop("platform_manifest_digest")

        recorded_at = d.pop("recorded_at")

        def _parse_registry_manifest_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest"))


        runtime_interface = cast(Literal['vonk.runtime.v1'] , d.pop("runtime_interface"))
        if runtime_interface != 'vonk.runtime.v1':
            raise ValueError(f"runtime_interface must match const 'vonk.runtime.v1', got '{runtime_interface}'")

        runtime_interface_label = cast(Literal['v1'] , d.pop("runtime_interface_label"))
        if runtime_interface_label != 'v1':
            raise ValueError(f"runtime_interface_label must match const 'v1', got '{runtime_interface_label}'")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source = check_runtime_image_receipt_source(d.pop("source"))




        runtime_image_receipt = cls(
            architecture=architecture,
            archive_path=archive_path,
            build_id=build_id,
            distribution_content_sha256=distribution_content_sha256,
            distribution_publisher=distribution_publisher,
            distribution_slug=distribution_slug,
            image_bytes=image_bytes,
            image_digest=image_digest,
            local_image_config_id=local_image_config_id,
            local_image_reference=local_image_reference,
            oci_archive_sha256=oci_archive_sha256,
            platform_manifest_digest=platform_manifest_digest,
            recorded_at=recorded_at,
            registry_manifest_digest=registry_manifest_digest,
            runtime_interface=runtime_interface,
            runtime_interface_label=runtime_interface_label,
            schema_version=schema_version,
            source=source,
        )

        return runtime_image_receipt
