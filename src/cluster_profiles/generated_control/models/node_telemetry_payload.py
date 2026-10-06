from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="NodeTelemetryPayload")



@_attrs_define
class NodeTelemetryPayload:
    """
        Attributes:
            node_id (str):
            sample_id (str):
     """

    node_id: str
    sample_id: str





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        sample_id = self.sample_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "sample_id": sample_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        sample_id = d.pop("sample_id")

        node_telemetry_payload = cls(
            node_id=node_id,
            sample_id=sample_id,
        )

        return node_telemetry_payload
