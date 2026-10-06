from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledRuntimeImage")



@_attrs_define
class CompiledRuntimeImage:
    """ A runtime image in the Controller's layered store.

    ``image_digest`` is its manifest digest and ``oci_layout_sha256`` the same
    digest's hex, its address in the store; ``image_bytes`` is the size of its
    layers. Sparks pull it into Docker as
    ``localhost/vonk/compiled-runtime-<oci_layout_sha256>@<image_digest>``.

        Attributes:
            build_id (str):
            image_bytes (int):
            image_digest (str):
            local_image_config_id (str):
            oci_layout_sha256 (str):
            runtime_interface_label (str):
     """

    build_id: str
    image_bytes: int
    image_digest: str
    local_image_config_id: str
    oci_layout_sha256: str
    runtime_interface_label: str





    def to_dict(self) -> dict[str, Any]:
        build_id = self.build_id

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        local_image_config_id = self.local_image_config_id

        oci_layout_sha256 = self.oci_layout_sha256

        runtime_interface_label = self.runtime_interface_label


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "local_image_config_id": local_image_config_id,
            "oci_layout_sha256": oci_layout_sha256,
            "runtime_interface_label": runtime_interface_label,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_id = d.pop("build_id")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        local_image_config_id = d.pop("local_image_config_id")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        runtime_interface_label = d.pop("runtime_interface_label")

        compiled_runtime_image = cls(
            build_id=build_id,
            image_bytes=image_bytes,
            image_digest=image_digest,
            local_image_config_id=local_image_config_id,
            oci_layout_sha256=oci_layout_sha256,
            runtime_interface_label=runtime_interface_label,
        )

        return compiled_runtime_image
