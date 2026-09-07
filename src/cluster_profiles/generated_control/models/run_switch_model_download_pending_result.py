from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_child_progress import RunSwitchChildProgress





T = TypeVar("T", bound="RunSwitchModelDownloadPendingResult")



@_attrs_define
class RunSwitchModelDownloadPendingResult:
    """
        Attributes:
            artifact_set_sha256 (str):
            downloaded_bytes (int):
            phase (Literal['transfer']):
            progress (RunSwitchChildProgress): Progress nested in a durable child receipt.
            subphase (Literal['model-download']):
            reason (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            total_bytes (Union[None, Unset, int]):
     """

    artifact_set_sha256: str
    downloaded_bytes: int
    phase: Literal['transfer']
    progress: 'RunSwitchChildProgress'
    subphase: Literal['model-download']
    reason: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_child_progress import RunSwitchChildProgress
        artifact_set_sha256 = self.artifact_set_sha256

        downloaded_bytes = self.downloaded_bytes

        phase = self.phase

        progress = self.progress.to_dict()

        subphase = self.subphase

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        schema_version = self.schema_version

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "downloaded_bytes": downloaded_bytes,
            "phase": phase,
            "progress": progress,
            "subphase": subphase,
        })
        if reason is not UNSET:
            field_dict["reason"] = reason
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_child_progress import RunSwitchChildProgress
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        downloaded_bytes = d.pop("downloaded_bytes")

        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        progress = RunSwitchChildProgress.from_dict(d.pop("progress"))




        subphase = cast(Literal['model-download'] , d.pop("subphase"))
        if subphase != 'model-download':
            raise ValueError(f"subphase must match const 'model-download', got '{subphase}'")

        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        run_switch_model_download_pending_result = cls(
            artifact_set_sha256=artifact_set_sha256,
            downloaded_bytes=downloaded_bytes,
            phase=phase,
            progress=progress,
            subphase=subphase,
            reason=reason,
            schema_version=schema_version,
            total_bytes=total_bytes,
        )

        return run_switch_model_download_pending_result
