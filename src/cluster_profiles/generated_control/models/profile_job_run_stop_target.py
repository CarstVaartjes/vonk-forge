from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ProfileJobRunStopTarget")



@_attrs_define
class ProfileJobRunStopTarget:
    """ One exact transient runtime and its durable JobRun source.

        Attributes:
            artifact_job_id (str):
            node_id (str):
            source_job_id (str):
            source_operation_id (str):
            stop_payload_sha256 (str):
     """

    artifact_job_id: str
    node_id: str
    source_job_id: str
    source_operation_id: str
    stop_payload_sha256: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        artifact_job_id = self.artifact_job_id

        node_id = self.node_id

        source_job_id = self.source_job_id

        source_operation_id = self.source_operation_id

        stop_payload_sha256 = self.stop_payload_sha256


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "artifact_job_id": artifact_job_id,
            "node_id": node_id,
            "source_job_id": source_job_id,
            "source_operation_id": source_operation_id,
            "stop_payload_sha256": stop_payload_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_job_id = d.pop("artifact_job_id")

        node_id = d.pop("node_id")

        source_job_id = d.pop("source_job_id")

        source_operation_id = d.pop("source_operation_id")

        stop_payload_sha256 = d.pop("stop_payload_sha256")

        profile_job_run_stop_target = cls(
            artifact_job_id=artifact_job_id,
            node_id=node_id,
            source_job_id=source_job_id,
            source_operation_id=source_operation_id,
            stop_payload_sha256=stop_payload_sha256,
        )


        profile_job_run_stop_target.additional_properties = d
        return profile_job_run_stop_target

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
