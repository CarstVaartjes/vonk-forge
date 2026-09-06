from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ArtifactJobStorageCapabilities")



@_attrs_define
class ArtifactJobStorageCapabilities:
    """
        Attributes:
            in_flight_uploads (int):
            max_stored_bytes (int):
            remaining_bytes (int):
            reserved_bytes (int):
            used_bytes (int):
     """

    in_flight_uploads: int
    max_stored_bytes: int
    remaining_bytes: int
    reserved_bytes: int
    used_bytes: int





    def to_dict(self) -> dict[str, Any]:
        in_flight_uploads = self.in_flight_uploads

        max_stored_bytes = self.max_stored_bytes

        remaining_bytes = self.remaining_bytes

        reserved_bytes = self.reserved_bytes

        used_bytes = self.used_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "in_flight_uploads": in_flight_uploads,
            "max_stored_bytes": max_stored_bytes,
            "remaining_bytes": remaining_bytes,
            "reserved_bytes": reserved_bytes,
            "used_bytes": used_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        in_flight_uploads = d.pop("in_flight_uploads")

        max_stored_bytes = d.pop("max_stored_bytes")

        remaining_bytes = d.pop("remaining_bytes")

        reserved_bytes = d.pop("reserved_bytes")

        used_bytes = d.pop("used_bytes")

        artifact_job_storage_capabilities = cls(
            in_flight_uploads=in_flight_uploads,
            max_stored_bytes=max_stored_bytes,
            remaining_bytes=remaining_bytes,
            reserved_bytes=reserved_bytes,
            used_bytes=used_bytes,
        )

        return artifact_job_storage_capabilities
