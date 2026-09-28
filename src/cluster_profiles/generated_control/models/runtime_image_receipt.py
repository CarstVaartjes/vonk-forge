from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
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
            build_id (str):
            distribution_content_sha256 (str):
            distribution_publisher (str):
            distribution_slug (str):
            image_bytes (int):
            image_digest (str):
            local_image_config_id (str):
            oci_archive_sha256 (str):
            recorded_at (str):
            runtime_adapter (str):
            runtime_adapter_sha256 (str):
            runtime_interface (Literal['vonk.runtime.v1']):
            runtime_interface_label (Literal['v1']):
            schema_version (Literal[2]):
            build_input_sha256 (None | str | Unset):
     """

    architecture: Literal['linux-arm64']
    archive_path: str
    build_id: str
    distribution_content_sha256: str
    distribution_publisher: str
    distribution_slug: str
    image_bytes: int
    image_digest: str
    local_image_config_id: str
    oci_archive_sha256: str
    recorded_at: str
    runtime_adapter: str
    runtime_adapter_sha256: str
    runtime_interface: Literal['vonk.runtime.v1']
    runtime_interface_label: Literal['v1']
    schema_version: Literal[2]
    build_input_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        architecture = self.architecture

        archive_path = self.archive_path

        build_id = self.build_id

        distribution_content_sha256 = self.distribution_content_sha256

        distribution_publisher = self.distribution_publisher

        distribution_slug = self.distribution_slug

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        local_image_config_id = self.local_image_config_id

        oci_archive_sha256 = self.oci_archive_sha256

        recorded_at = self.recorded_at

        runtime_adapter = self.runtime_adapter

        runtime_adapter_sha256 = self.runtime_adapter_sha256

        runtime_interface = self.runtime_interface

        runtime_interface_label = self.runtime_interface_label

        schema_version = self.schema_version

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256


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
            "oci_archive_sha256": oci_archive_sha256,
            "recorded_at": recorded_at,
            "runtime_adapter": runtime_adapter,
            "runtime_adapter_sha256": runtime_adapter_sha256,
            "runtime_interface": runtime_interface,
            "runtime_interface_label": runtime_interface_label,
            "schema_version": schema_version,
        })
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        archive_path = d.pop("archive_path")

        build_id = d.pop("build_id")

        distribution_content_sha256 = d.pop("distribution_content_sha256")

        distribution_publisher = d.pop("distribution_publisher")

        distribution_slug = d.pop("distribution_slug")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        local_image_config_id = d.pop("local_image_config_id")

        oci_archive_sha256 = d.pop("oci_archive_sha256")

        recorded_at = d.pop("recorded_at")

        runtime_adapter = d.pop("runtime_adapter")

        runtime_adapter_sha256 = d.pop("runtime_adapter_sha256")

        runtime_interface = cast(Literal['vonk.runtime.v1'] , d.pop("runtime_interface"))
        if runtime_interface != 'vonk.runtime.v1':
            raise ValueError(f"runtime_interface must match const 'vonk.runtime.v1', got '{runtime_interface}'")

        runtime_interface_label = cast(Literal['v1'] , d.pop("runtime_interface_label"))
        if runtime_interface_label != 'v1':
            raise ValueError(f"runtime_interface_label must match const 'v1', got '{runtime_interface_label}'")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


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
            oci_archive_sha256=oci_archive_sha256,
            recorded_at=recorded_at,
            runtime_adapter=runtime_adapter,
            runtime_adapter_sha256=runtime_adapter_sha256,
            runtime_interface=runtime_interface,
            runtime_interface_label=runtime_interface_label,
            schema_version=schema_version,
            build_input_sha256=build_input_sha256,
        )

        return runtime_image_receipt
