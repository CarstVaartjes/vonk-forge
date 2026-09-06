from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ArtifactJobTransportCapabilities")



@_attrs_define
class ArtifactJobTransportCapabilities:
    """
        Attributes:
            max_input_file_bytes (int):
            max_input_files (int):
            max_input_total_bytes (int):
            max_output_file_bytes (int):
            max_output_files (int):
            max_output_total_bytes (int):
            max_timeout_seconds (int):
            reserved_input_names (list[str]):
     """

    max_input_file_bytes: int
    max_input_files: int
    max_input_total_bytes: int
    max_output_file_bytes: int
    max_output_files: int
    max_output_total_bytes: int
    max_timeout_seconds: int
    reserved_input_names: list[str]





    def to_dict(self) -> dict[str, Any]:
        max_input_file_bytes = self.max_input_file_bytes

        max_input_files = self.max_input_files

        max_input_total_bytes = self.max_input_total_bytes

        max_output_file_bytes = self.max_output_file_bytes

        max_output_files = self.max_output_files

        max_output_total_bytes = self.max_output_total_bytes

        max_timeout_seconds = self.max_timeout_seconds

        reserved_input_names = self.reserved_input_names




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "max_input_file_bytes": max_input_file_bytes,
            "max_input_files": max_input_files,
            "max_input_total_bytes": max_input_total_bytes,
            "max_output_file_bytes": max_output_file_bytes,
            "max_output_files": max_output_files,
            "max_output_total_bytes": max_output_total_bytes,
            "max_timeout_seconds": max_timeout_seconds,
            "reserved_input_names": reserved_input_names,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        max_input_file_bytes = d.pop("max_input_file_bytes")

        max_input_files = d.pop("max_input_files")

        max_input_total_bytes = d.pop("max_input_total_bytes")

        max_output_file_bytes = d.pop("max_output_file_bytes")

        max_output_files = d.pop("max_output_files")

        max_output_total_bytes = d.pop("max_output_total_bytes")

        max_timeout_seconds = d.pop("max_timeout_seconds")

        reserved_input_names = cast(list[str], d.pop("reserved_input_names"))


        artifact_job_transport_capabilities = cls(
            max_input_file_bytes=max_input_file_bytes,
            max_input_files=max_input_files,
            max_input_total_bytes=max_input_total_bytes,
            max_output_file_bytes=max_output_file_bytes,
            max_output_files=max_output_files,
            max_output_total_bytes=max_output_total_bytes,
            max_timeout_seconds=max_timeout_seconds,
            reserved_input_names=reserved_input_names,
        )

        return artifact_job_transport_capabilities
