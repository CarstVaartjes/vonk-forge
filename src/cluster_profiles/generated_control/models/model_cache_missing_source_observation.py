from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_missing_source_observation_status import check_model_cache_missing_source_observation_status
from ..models.model_cache_missing_source_observation_status import ModelCacheMissingSourceObservationStatus
from typing import cast






T = TypeVar("T", bound="ModelCacheMissingSourceObservation")



@_attrs_define
class ModelCacheMissingSourceObservation:
    """
        Attributes:
            artifact_key (str):
            observations (int):
            status (ModelCacheMissingSourceObservationStatus):
     """

    artifact_key: str
    observations: int
    status: ModelCacheMissingSourceObservationStatus





    def to_dict(self) -> dict[str, Any]:
        artifact_key = self.artifact_key

        observations = self.observations

        status: int = self.status


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_key": artifact_key,
            "observations": observations,
            "status": status,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_key = d.pop("artifact_key")

        observations = d.pop("observations")

        status = check_model_cache_missing_source_observation_status(d.pop("status"))




        model_cache_missing_source_observation = cls(
            artifact_key=artifact_key,
            observations=observations,
            status=status,
        )

        return model_cache_missing_source_observation
