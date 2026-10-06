from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.runtime_preflight_request_fabric_connectivity import check_runtime_preflight_request_fabric_connectivity
from ..models.runtime_preflight_request_fabric_connectivity import RuntimePreflightRequestFabricConnectivity
from typing import cast






T = TypeVar("T", bound="RuntimePreflightRequest")



@_attrs_define
class RuntimePreflightRequest:
    """ Check this linux/arm64 host can run one recipe runtime.

        Attributes:
            fabric_connectivity (RuntimePreflightRequestFabricConnectivity):
            fabric_minimum_mbps (int):
            minimum_free_bytes (int):
            source_build (bool):
     """

    fabric_connectivity: RuntimePreflightRequestFabricConnectivity
    fabric_minimum_mbps: int
    minimum_free_bytes: int
    source_build: bool





    def to_dict(self) -> dict[str, Any]:
        fabric_connectivity: str = self.fabric_connectivity

        fabric_minimum_mbps = self.fabric_minimum_mbps

        minimum_free_bytes = self.minimum_free_bytes

        source_build = self.source_build


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "fabric_connectivity": fabric_connectivity,
            "fabric_minimum_mbps": fabric_minimum_mbps,
            "minimum_free_bytes": minimum_free_bytes,
            "source_build": source_build,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        fabric_connectivity = check_runtime_preflight_request_fabric_connectivity(d.pop("fabric_connectivity"))




        fabric_minimum_mbps = d.pop("fabric_minimum_mbps")

        minimum_free_bytes = d.pop("minimum_free_bytes")

        source_build = d.pop("source_build")

        runtime_preflight_request = cls(
            fabric_connectivity=fabric_connectivity,
            fabric_minimum_mbps=fabric_minimum_mbps,
            minimum_free_bytes=minimum_free_bytes,
            source_build=source_build,
        )

        return runtime_preflight_request
