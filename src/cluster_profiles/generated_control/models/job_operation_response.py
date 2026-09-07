from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.operation_evidence_provenance import OperationEvidenceProvenance
  from ..models.operation_evidence_download import OperationEvidenceDownload
  from ..models.job_operation_progress import JobOperationProgress
  from ..models.operation_failure_evidence import OperationFailureEvidence
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
            evidence_download (Union['OperationEvidenceDownload', None, Unset]):
            failure (Union['OperationFailureEvidence', None, Unset]):
            graph_operation_id (Union[None, Unset, str]):
            progress (Union['JobOperationProgress', None, Unset]):
            provenance (Union['OperationEvidenceProvenance', None, Unset]):
            recovery (Union['OperationRecovery', None, Unset]):
            updated_at (Union[None, Unset, str]):
     """

    attempt: int
    id: str
    kind: str
    node_id: str
    state: str
    evidence_download: Union['OperationEvidenceDownload', None, Unset] = UNSET
    failure: Union['OperationFailureEvidence', None, Unset] = UNSET
    graph_operation_id: Union[None, Unset, str] = UNSET
    progress: Union['JobOperationProgress', None, Unset] = UNSET
    provenance: Union['OperationEvidenceProvenance', None, Unset] = UNSET
    recovery: Union['OperationRecovery', None, Unset] = UNSET
    updated_at: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_evidence_provenance import OperationEvidenceProvenance
        from ..models.operation_evidence_download import OperationEvidenceDownload
        from ..models.job_operation_progress import JobOperationProgress
        from ..models.operation_failure_evidence import OperationFailureEvidence
        from ..models.operation_recovery import OperationRecovery
        attempt = self.attempt

        id = self.id

        kind = self.kind

        node_id = self.node_id

        state = self.state

        evidence_download: Union[None, Unset, dict[str, Any]]
        if isinstance(self.evidence_download, Unset):
            evidence_download = UNSET
        elif isinstance(self.evidence_download, OperationEvidenceDownload):
            evidence_download = self.evidence_download.to_dict()
        else:
            evidence_download = self.evidence_download

        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, OperationFailureEvidence):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        graph_operation_id: Union[None, Unset, str]
        if isinstance(self.graph_operation_id, Unset):
            graph_operation_id = UNSET
        else:
            graph_operation_id = self.graph_operation_id

        progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, JobOperationProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        provenance: Union[None, Unset, dict[str, Any]]
        if isinstance(self.provenance, Unset):
            provenance = UNSET
        elif isinstance(self.provenance, OperationEvidenceProvenance):
            provenance = self.provenance.to_dict()
        else:
            provenance = self.provenance

        recovery: Union[None, Unset, dict[str, Any]]
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        elif isinstance(self.recovery, OperationRecovery):
            recovery = self.recovery.to_dict()
        else:
            recovery = self.recovery

        updated_at: Union[None, Unset, str]
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
        if graph_operation_id is not UNSET:
            field_dict["graph_operation_id"] = graph_operation_id
        if progress is not UNSET:
            field_dict["progress"] = progress
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_evidence_provenance import OperationEvidenceProvenance
        from ..models.operation_evidence_download import OperationEvidenceDownload
        from ..models.job_operation_progress import JobOperationProgress
        from ..models.operation_failure_evidence import OperationFailureEvidence
        from ..models.operation_recovery import OperationRecovery
        d = dict(src_dict)
        attempt = d.pop("attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        node_id = d.pop("node_id")

        state = d.pop("state")

        def _parse_evidence_download(data: object) -> Union['OperationEvidenceDownload', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_download_type_0 = OperationEvidenceDownload.from_dict(data)



                return evidence_download_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationEvidenceDownload', None, Unset], data)

        evidence_download = _parse_evidence_download(d.pop("evidence_download", UNSET))


        def _parse_failure(data: object) -> Union['OperationFailureEvidence', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = OperationFailureEvidence.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationFailureEvidence', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_graph_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        graph_operation_id = _parse_graph_operation_id(d.pop("graph_operation_id", UNSET))


        def _parse_progress(data: object) -> Union['JobOperationProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = JobOperationProgress.from_dict(data)



                return progress_type_0
            except: # noqa: E722
                pass
            return cast(Union['JobOperationProgress', None, Unset], data)

        progress = _parse_progress(d.pop("progress", UNSET))


        def _parse_provenance(data: object) -> Union['OperationEvidenceProvenance', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                provenance_type_0 = OperationEvidenceProvenance.from_dict(data)



                return provenance_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationEvidenceProvenance', None, Unset], data)

        provenance = _parse_provenance(d.pop("provenance", UNSET))


        def _parse_recovery(data: object) -> Union['OperationRecovery', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recovery_type_0 = OperationRecovery.from_dict(data)



                return recovery_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationRecovery', None, Unset], data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        def _parse_updated_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))


        job_operation_response = cls(
            attempt=attempt,
            id=id,
            kind=kind,
            node_id=node_id,
            state=state,
            evidence_download=evidence_download,
            failure=failure,
            graph_operation_id=graph_operation_id,
            progress=progress,
            provenance=provenance,
            recovery=recovery,
            updated_at=updated_at,
        )

        return job_operation_response
