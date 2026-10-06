from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelCacheTransferArtifact")



@_attrs_define
class ModelCacheTransferArtifact:
    """
        Attributes:
            baseline_bytes (int):
            received_bytes (int):
            started_at (str):
     """

    baseline_bytes: int
    received_bytes: int
    started_at: str





    def to_dict(self) -> dict[str, Any]:
        baseline_bytes = self.baseline_bytes

        received_bytes = self.received_bytes

        started_at = self.started_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "baseline_bytes": baseline_bytes,
            "received_bytes": received_bytes,
            "started_at": started_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        baseline_bytes = d.pop("baseline_bytes")

        received_bytes = d.pop("received_bytes")

        started_at = d.pop("started_at")

        model_cache_transfer_artifact = cls(
            baseline_bytes=baseline_bytes,
            received_bytes=received_bytes,
            started_at=started_at,
        )

        return model_cache_transfer_artifact
