from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.model_cache_download_result import ModelCacheDownloadResult
  from ..models.run_switch_child_progress import RunSwitchChildProgress





T = TypeVar("T", bound="RunSwitchModelDownloadResult")



@_attrs_define
class RunSwitchModelDownloadResult:
    """
        Attributes:
            artifact_set_sha256 (str):
            coverage (Literal['complete']):
            downloaded_bytes (int):
            phase (Literal['transfer']):
            progress (RunSwitchChildProgress): Progress nested in a durable child receipt.
            schema_version (Literal[2]):
            subphase (Literal['model-download']):
            evidence (Union['ModelCacheDownloadResult', None, Unset]):
            reason (Union[None, Unset, str]):
            skipped (Union[Unset, bool]):  Default: True.
            total_bytes (Union[None, Unset, int]):
     """

    artifact_set_sha256: str
    coverage: Literal['complete']
    downloaded_bytes: int
    phase: Literal['transfer']
    progress: 'RunSwitchChildProgress'
    schema_version: Literal[2]
    subphase: Literal['model-download']
    evidence: Union['ModelCacheDownloadResult', None, Unset] = UNSET
    reason: Union[None, Unset, str] = UNSET
    skipped: Union[Unset, bool] = True
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_download_result import ModelCacheDownloadResult
        from ..models.run_switch_child_progress import RunSwitchChildProgress
        artifact_set_sha256 = self.artifact_set_sha256

        coverage = self.coverage

        downloaded_bytes = self.downloaded_bytes

        phase = self.phase

        progress = self.progress.to_dict()

        schema_version = self.schema_version

        subphase = self.subphase

        evidence: Union[None, Unset, dict[str, Any]]
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        elif isinstance(self.evidence, ModelCacheDownloadResult):
            evidence = self.evidence.to_dict()
        else:
            evidence = self.evidence

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        skipped = self.skipped

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "coverage": coverage,
            "downloaded_bytes": downloaded_bytes,
            "phase": phase,
            "progress": progress,
            "schema_version": schema_version,
            "subphase": subphase,
        })
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if reason is not UNSET:
            field_dict["reason"] = reason
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_cache_download_result import ModelCacheDownloadResult
        from ..models.run_switch_child_progress import RunSwitchChildProgress
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        coverage = cast(Literal['complete'] , d.pop("coverage"))
        if coverage != 'complete':
            raise ValueError(f"coverage must match const 'complete', got '{coverage}'")

        downloaded_bytes = d.pop("downloaded_bytes")

        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        progress = RunSwitchChildProgress.from_dict(d.pop("progress"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        subphase = cast(Literal['model-download'] , d.pop("subphase"))
        if subphase != 'model-download':
            raise ValueError(f"subphase must match const 'model-download', got '{subphase}'")

        def _parse_evidence(data: object) -> Union['ModelCacheDownloadResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_type_0 = ModelCacheDownloadResult.from_dict(data)



                return evidence_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheDownloadResult', None, Unset], data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        skipped = d.pop("skipped", UNSET)

        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        run_switch_model_download_result = cls(
            artifact_set_sha256=artifact_set_sha256,
            coverage=coverage,
            downloaded_bytes=downloaded_bytes,
            phase=phase,
            progress=progress,
            schema_version=schema_version,
            subphase=subphase,
            evidence=evidence,
            reason=reason,
            skipped=skipped,
            total_bytes=total_bytes,
        )

        return run_switch_model_download_result
