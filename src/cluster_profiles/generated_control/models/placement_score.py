from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PlacementScore")



@_attrs_define
class PlacementScore:
    """
        Attributes:
            active_run_count (int):
            artifact_reuse_bytes (int):
            exact_install_complete (bool):
            exact_install_partial (bool):
            maximum_telemetry_age_seconds (float):
            minimum_disk_headroom_bytes (int):
            minimum_memory_headroom_bytes (int):
     """

    active_run_count: int
    artifact_reuse_bytes: int
    exact_install_complete: bool
    exact_install_partial: bool
    maximum_telemetry_age_seconds: float
    minimum_disk_headroom_bytes: int
    minimum_memory_headroom_bytes: int





    def to_dict(self) -> dict[str, Any]:
        active_run_count = self.active_run_count

        artifact_reuse_bytes = self.artifact_reuse_bytes

        exact_install_complete = self.exact_install_complete

        exact_install_partial = self.exact_install_partial

        maximum_telemetry_age_seconds = self.maximum_telemetry_age_seconds

        minimum_disk_headroom_bytes = self.minimum_disk_headroom_bytes

        minimum_memory_headroom_bytes = self.minimum_memory_headroom_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_run_count": active_run_count,
            "artifact_reuse_bytes": artifact_reuse_bytes,
            "exact_install_complete": exact_install_complete,
            "exact_install_partial": exact_install_partial,
            "maximum_telemetry_age_seconds": maximum_telemetry_age_seconds,
            "minimum_disk_headroom_bytes": minimum_disk_headroom_bytes,
            "minimum_memory_headroom_bytes": minimum_memory_headroom_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        active_run_count = d.pop("active_run_count")

        artifact_reuse_bytes = d.pop("artifact_reuse_bytes")

        exact_install_complete = d.pop("exact_install_complete")

        exact_install_partial = d.pop("exact_install_partial")

        maximum_telemetry_age_seconds = d.pop("maximum_telemetry_age_seconds")

        minimum_disk_headroom_bytes = d.pop("minimum_disk_headroom_bytes")

        minimum_memory_headroom_bytes = d.pop("minimum_memory_headroom_bytes")

        placement_score = cls(
            active_run_count=active_run_count,
            artifact_reuse_bytes=artifact_reuse_bytes,
            exact_install_complete=exact_install_complete,
            exact_install_partial=exact_install_partial,
            maximum_telemetry_age_seconds=maximum_telemetry_age_seconds,
            minimum_disk_headroom_bytes=minimum_disk_headroom_bytes,
            minimum_memory_headroom_bytes=minimum_memory_headroom_bytes,
        )

        return placement_score
