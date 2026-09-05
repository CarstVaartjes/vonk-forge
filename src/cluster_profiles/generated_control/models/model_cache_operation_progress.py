from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_operation_progress_phase import check_model_cache_operation_progress_phase
from ..models.model_cache_operation_progress_phase import ModelCacheOperationProgressPhase
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="ModelCacheOperationProgress")



@_attrs_define
class ModelCacheOperationProgress:
    """
        Attributes:
            completed_artifacts (int):
            downloaded_bytes (int):
            expected_bytes (int):
            phase (ModelCacheOperationProgressPhase):
            total_artifacts (int):
            current_artifact_key (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    completed_artifacts: int
    downloaded_bytes: int
    expected_bytes: int
    phase: ModelCacheOperationProgressPhase
    total_artifacts: int
    current_artifact_key: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        completed_artifacts = self.completed_artifacts

        downloaded_bytes = self.downloaded_bytes

        expected_bytes = self.expected_bytes

        phase: str = self.phase

        total_artifacts = self.total_artifacts

        current_artifact_key: Union[None, Unset, str]
        if isinstance(self.current_artifact_key, Unset):
            current_artifact_key = UNSET
        else:
            current_artifact_key = self.current_artifact_key

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_artifacts": completed_artifacts,
            "downloaded_bytes": downloaded_bytes,
            "expected_bytes": expected_bytes,
            "phase": phase,
            "total_artifacts": total_artifacts,
        })
        if current_artifact_key is not UNSET:
            field_dict["current_artifact_key"] = current_artifact_key
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        completed_artifacts = d.pop("completed_artifacts")

        downloaded_bytes = d.pop("downloaded_bytes")

        expected_bytes = d.pop("expected_bytes")

        phase = check_model_cache_operation_progress_phase(d.pop("phase"))




        total_artifacts = d.pop("total_artifacts")

        def _parse_current_artifact_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        current_artifact_key = _parse_current_artifact_key(d.pop("current_artifact_key", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_operation_progress = cls(
            completed_artifacts=completed_artifacts,
            downloaded_bytes=downloaded_bytes,
            expected_bytes=expected_bytes,
            phase=phase,
            total_artifacts=total_artifacts,
            current_artifact_key=current_artifact_key,
            schema_version=schema_version,
        )

        return model_cache_operation_progress
