from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_failure_result import AgentFailureResult
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.operation_evidence_download import OperationEvidenceDownload
  from ..models.operation_failure_evidence import OperationFailureEvidence
  from ..models.operation_progress import OperationProgress
  from ..models.operation_recovery import OperationRecovery





T = TypeVar("T", bound="JobOperationResponse")



@_attrs_define
class JobOperationResponse:
    """
        Attributes:
            attempt (int):
            id (str):
            kind (str):
            node_id (str):
            state (str):
            evidence_download (None | OperationEvidenceDownload | Unset):
            failure (AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset):
            progress (None | OperationProgress | Unset):
            recovery (None | OperationRecovery | Unset):
            updated_at (None | str | Unset):
     """

    attempt: int
    id: str
    kind: str
    node_id: str
    state: str
    evidence_download: None | OperationEvidenceDownload | Unset = UNSET
    failure: AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset = UNSET
    progress: None | OperationProgress | Unset = UNSET
    recovery: None | OperationRecovery | Unset = UNSET
    updated_at: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_failure_result import AgentFailureResult # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_evidence_download import OperationEvidenceDownload # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        attempt = self.attempt

        id = self.id

        kind = self.kind

        node_id = self.node_id

        state = self.state

        evidence_download: dict[str, Any] | None | Unset
        if isinstance(self.evidence_download, Unset):
            evidence_download = UNSET
        elif isinstance(self.evidence_download, OperationEvidenceDownload):
            evidence_download = self.evidence_download.to_dict()
        else:
            evidence_download = self.evidence_download

        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AgentFailureResult):
            failure = self.failure.to_dict()
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        elif isinstance(self.failure, OperationFailureEvidence):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        progress: dict[str, Any] | None | Unset
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, OperationProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        recovery: dict[str, Any] | None | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        elif isinstance(self.recovery, OperationRecovery):
            recovery = self.recovery.to_dict()
        else:
            recovery = self.recovery

        updated_at: None | str | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "id": id,
            "kind": kind,
            "node_id": node_id,
            "state": state,
        })
        if evidence_download is not UNSET:
            field_dict["evidence_download"] = evidence_download
        if failure is not UNSET:
            field_dict["failure"] = failure
        if progress is not UNSET:
            field_dict["progress"] = progress
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_failure_result import AgentFailureResult # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_evidence_download import OperationEvidenceDownload # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        d = dict(src_dict)
        attempt = d.pop("attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        node_id = d.pop("node_id")

        state = d.pop("state")

        def _parse_evidence_download(data: object) -> None | OperationEvidenceDownload | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_download_type_0 = OperationEvidenceDownload.from_dict(data)



                return evidence_download_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationEvidenceDownload | Unset, data)

        evidence_download = _parse_evidence_download(d.pop("evidence_download", UNSET))


        def _parse_failure(data: object) -> AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AgentFailureResult.from_dict(data)



                return failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_1 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_2 = OperationFailureEvidence.from_dict(data)



                return failure_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset, data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_progress(data: object) -> None | OperationProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = OperationProgress.from_dict(data)



                return progress_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationProgress | Unset, data)

        progress = _parse_progress(d.pop("progress", UNSET))


        def _parse_recovery(data: object) -> None | OperationRecovery | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recovery_type_0 = OperationRecovery.from_dict(data)



                return recovery_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationRecovery | Unset, data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        def _parse_updated_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))


        job_operation_response = cls(
            attempt=attempt,
            id=id,
            kind=kind,
            node_id=node_id,
            state=state,
            evidence_download=evidence_download,
            failure=failure,
            progress=progress,
            recovery=recovery,
            updated_at=updated_at,
        )

        return job_operation_response
