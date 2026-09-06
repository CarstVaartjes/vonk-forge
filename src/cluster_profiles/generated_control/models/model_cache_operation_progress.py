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

if TYPE_CHECKING:
  from ..models.operation_member_progress import OperationMemberProgress





T = TypeVar("T", bound="ModelCacheOperationProgress")



@_attrs_define
class ModelCacheOperationProgress:
    """
        Attributes:
            completed_artifacts (int):
            downloaded_bytes (int):
            phase (ModelCacheOperationProgressPhase):
            total_artifacts (int):
            bytes_per_second (Union[None, Unset, float]):
            current_artifact_key (Union[None, Unset, str]):
            eta_seconds (Union[None, Unset, float]):
            expected_bytes (Union[None, Unset, int]):
            members (Union[Unset, list['OperationMemberProgress']]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            total_bytes_known (Union[Unset, bool]):  Default: True.
     """

    completed_artifacts: int
    downloaded_bytes: int
    phase: ModelCacheOperationProgressPhase
    total_artifacts: int
    bytes_per_second: Union[None, Unset, float] = UNSET
    current_artifact_key: Union[None, Unset, str] = UNSET
    eta_seconds: Union[None, Unset, float] = UNSET
    expected_bytes: Union[None, Unset, int] = UNSET
    members: Union[Unset, list['OperationMemberProgress']] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    total_bytes_known: Union[Unset, bool] = True





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_member_progress import OperationMemberProgress
        completed_artifacts = self.completed_artifacts

        downloaded_bytes = self.downloaded_bytes

        phase: str = self.phase

        total_artifacts = self.total_artifacts

        bytes_per_second: Union[None, Unset, float]
        if isinstance(self.bytes_per_second, Unset):
            bytes_per_second = UNSET
        else:
            bytes_per_second = self.bytes_per_second

        current_artifact_key: Union[None, Unset, str]
        if isinstance(self.current_artifact_key, Unset):
            current_artifact_key = UNSET
        else:
            current_artifact_key = self.current_artifact_key

        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        expected_bytes: Union[None, Unset, int]
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes

        members: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.members, Unset):
            members = []
            for members_item_data in self.members:
                members_item = members_item_data.to_dict()
                members.append(members_item)



        schema_version = self.schema_version

        total_bytes_known = self.total_bytes_known


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_artifacts": completed_artifacts,
            "downloaded_bytes": downloaded_bytes,
            "phase": phase,
            "total_artifacts": total_artifacts,
        })
        if bytes_per_second is not UNSET:
            field_dict["bytes_per_second"] = bytes_per_second
        if current_artifact_key is not UNSET:
            field_dict["current_artifact_key"] = current_artifact_key
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if expected_bytes is not UNSET:
            field_dict["expected_bytes"] = expected_bytes
        if members is not UNSET:
            field_dict["members"] = members
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if total_bytes_known is not UNSET:
            field_dict["total_bytes_known"] = total_bytes_known

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_member_progress import OperationMemberProgress
        d = dict(src_dict)
        completed_artifacts = d.pop("completed_artifacts")

        downloaded_bytes = d.pop("downloaded_bytes")

        phase = check_model_cache_operation_progress_phase(d.pop("phase"))




        total_artifacts = d.pop("total_artifacts")

        def _parse_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        bytes_per_second = _parse_bytes_per_second(d.pop("bytes_per_second", UNSET))


        def _parse_current_artifact_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        current_artifact_key = _parse_current_artifact_key(d.pop("current_artifact_key", UNSET))


        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_expected_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        members = []
        _members = d.pop("members", UNSET)
        for members_item_data in (_members or []):
            members_item = OperationMemberProgress.from_dict(members_item_data)



            members.append(members_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        total_bytes_known = d.pop("total_bytes_known", UNSET)

        model_cache_operation_progress = cls(
            completed_artifacts=completed_artifacts,
            downloaded_bytes=downloaded_bytes,
            phase=phase,
            total_artifacts=total_artifacts,
            bytes_per_second=bytes_per_second,
            current_artifact_key=current_artifact_key,
            eta_seconds=eta_seconds,
            expected_bytes=expected_bytes,
            members=members,
            schema_version=schema_version,
            total_bytes_known=total_bytes_known,
        )

        return model_cache_operation_progress
