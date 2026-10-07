from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime






T = TypeVar("T", bound="WorkerRuntimeObservation")



@_attrs_define
class WorkerRuntimeObservation:
    """
        Attributes:
            completed_at (datetime.datetime):
            loop_sequence (int):
            process_instance_id (str):
            source_sha (None | str):
            worker_contract_sha256 (None | str):
     """

    completed_at: datetime.datetime
    loop_sequence: int
    process_instance_id: str
    source_sha: None | str
    worker_contract_sha256: None | str





    def to_dict(self) -> dict[str, Any]:
        completed_at = self.completed_at.isoformat()

        loop_sequence = self.loop_sequence

        process_instance_id = self.process_instance_id

        source_sha: None | str
        source_sha = self.source_sha

        worker_contract_sha256: None | str
        worker_contract_sha256 = self.worker_contract_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_at": completed_at,
            "loop_sequence": loop_sequence,
            "process_instance_id": process_instance_id,
            "source_sha": source_sha,
            "worker_contract_sha256": worker_contract_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        completed_at = datetime.datetime.fromisoformat(d.pop("completed_at"))




        loop_sequence = d.pop("loop_sequence")

        process_instance_id = d.pop("process_instance_id")

        def _parse_source_sha(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        source_sha = _parse_source_sha(d.pop("source_sha"))


        def _parse_worker_contract_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        worker_contract_sha256 = _parse_worker_contract_sha256(d.pop("worker_contract_sha256"))


        worker_runtime_observation = cls(
            completed_at=completed_at,
            loop_sequence=loop_sequence,
            process_instance_id=process_instance_id,
            source_sha=source_sha,
            worker_contract_sha256=worker_contract_sha256,
        )

        return worker_runtime_observation
