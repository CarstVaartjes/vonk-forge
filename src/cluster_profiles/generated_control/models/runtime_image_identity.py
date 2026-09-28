from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RuntimeImageIdentity")



@_attrs_define
class RuntimeImageIdentity:
    """ Executable OCI identity, independent of its transfer observations.

        Attributes:
            architecture (Literal['linux-arm64']):
            image_bytes (int):
            image_digest (str):
            oci_layout_sha256 (str):
            runtime_interface (str):
            build_id (None | str | Unset):
     """

    architecture: Literal['linux-arm64']
    image_bytes: int
    image_digest: str
    oci_layout_sha256: str
    runtime_interface: str
    build_id: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        architecture = self.architecture

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_layout_sha256 = self.oci_layout_sha256

        runtime_interface = self.runtime_interface

        build_id: None | str | Unset
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_layout_sha256": oci_layout_sha256,
            "runtime_interface": runtime_interface,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        runtime_interface = d.pop("runtime_interface")

        def _parse_build_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        runtime_image_identity = cls(
            architecture=architecture,
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
            runtime_interface=runtime_interface,
            build_id=build_id,
        )

        return runtime_image_identity
