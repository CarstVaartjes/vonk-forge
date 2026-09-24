from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.operation_evidence_provenance import OperationEvidenceProvenance
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
  from ..models.operation_owner_reference import OperationOwnerReference
  from ..models.operation_evidence_download import OperationEvidenceDownload
  from ..models.operation_progress import OperationProgress
  from ..models.agent_failure_result import AgentFailureResult
  from ..models.operation_failure_evidence import OperationFailureEvidence
  from ..models.operation_recovery import OperationRecovery





T = TypeVar("T", bound="OperationDetailResponse")



@_attrs_define
class OperationDetailResponse:
    """
        Attributes:
            attempt (int):
            created_at (str):
            id (str):
            kind (str):
            node_ids (list[str]):
            state (str):
            cancellation (Union['FleetProfileApplicationCancellationView', None, Unset]):
            evidence_download (Union['OperationEvidenceDownload', None, Unset]):
            failure (Union['AgentFailureResult', 'AvailabilityOperationFailure', 'OperationFailureEvidence', None, Unset]):
            owner (Union['OperationOwnerReference', None, Unset]):
            parent_id (Union[None, Unset, str]):
            progress (Union['OperationProgress', None, Unset]):
            provenance (Union['OperationEvidenceProvenance', None, Unset]):
            recovery (Union['OperationRecovery', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            status_reason (Union[None, Unset, str]):
            updated_at (Union[None, Unset, str]):
     """

    attempt: int
    created_at: str
    id: str
    kind: str
    node_ids: list[str]
    state: str
    cancellation: Union['FleetProfileApplicationCancellationView', None, Unset] = UNSET
    evidence_download: Union['OperationEvidenceDownload', None, Unset] = UNSET
    failure: Union['AgentFailureResult', 'AvailabilityOperationFailure', 'OperationFailureEvidence', None, Unset] = UNSET
    owner: Union['OperationOwnerReference', None, Unset] = UNSET
    parent_id: Union[None, Unset, str] = UNSET
    progress: Union['OperationProgress', None, Unset] = UNSET
    provenance: Union['OperationEvidenceProvenance', None, Unset] = UNSET
    recovery: Union['OperationRecovery', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    status_reason: Union[None, Unset, str] = UNSET
    updated_at: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_evidence_provenance import OperationEvidenceProvenance
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
        from ..models.operation_owner_reference import OperationOwnerReference
        from ..models.operation_evidence_download import OperationEvidenceDownload
        from ..models.operation_progress import OperationProgress
        from ..models.agent_failure_result import AgentFailureResult
        from ..models.operation_failure_evidence import OperationFailureEvidence
        from ..models.operation_recovery import OperationRecovery
        attempt = self.attempt

        created_at = self.created_at

        id = self.id

        kind = self.kind

        node_ids = self.node_ids



        state = self.state

        cancellation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationView):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

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
        elif isinstance(self.failure, AgentFailureResult):
            failure = self.failure.to_dict()
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        elif isinstance(self.failure, OperationFailureEvidence):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        owner: Union[None, Unset, dict[str, Any]]
        if isinstance(self.owner, Unset):
            owner = UNSET
        elif isinstance(self.owner, OperationOwnerReference):
            owner = self.owner.to_dict()
        else:
            owner = self.owner

        parent_id: Union[None, Unset, str]
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, OperationProgress):
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

        schema_version = self.schema_version

        status_reason: Union[None, Unset, str]
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason

        updated_at: Union[None, Unset, str]
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "created_at": created_at,
            "id": id,
            "kind": kind,
            "node_ids": node_ids,
            "state": state,
        })
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if evidence_download is not UNSET:
            field_dict["evidence_download"] = evidence_download
        if failure is not UNSET:
            field_dict["failure"] = failure
        if owner is not UNSET:
            field_dict["owner"] = owner
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if progress is not UNSET:
            field_dict["progress"] = progress
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_evidence_provenance import OperationEvidenceProvenance
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
        from ..models.operation_owner_reference import OperationOwnerReference
        from ..models.operation_evidence_download import OperationEvidenceDownload
        from ..models.operation_progress import OperationProgress
        from ..models.agent_failure_result import AgentFailureResult
        from ..models.operation_failure_evidence import OperationFailureEvidence
        from ..models.operation_recovery import OperationRecovery
        d = dict(src_dict)
        attempt = d.pop("attempt")

        created_at = d.pop("created_at")

        id = d.pop("id")

        kind = d.pop("kind")

        node_ids = cast(list[str], d.pop("node_ids"))


        state = d.pop("state")

        def _parse_cancellation(data: object) -> Union['FleetProfileApplicationCancellationView', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = FleetProfileApplicationCancellationView.from_dict(data)



                return cancellation_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileApplicationCancellationView', None, Unset], data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


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


        def _parse_failure(data: object) -> Union['AgentFailureResult', 'AvailabilityOperationFailure', 'OperationFailureEvidence', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AgentFailureResult.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_1 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_1
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_2 = OperationFailureEvidence.from_dict(data)



                return failure_type_2
            except: # noqa: E722
                pass
            return cast(Union['AgentFailureResult', 'AvailabilityOperationFailure', 'OperationFailureEvidence', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_owner(data: object) -> Union['OperationOwnerReference', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                owner_type_0 = OperationOwnerReference.from_dict(data)



                return owner_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationOwnerReference', None, Unset], data)

        owner = _parse_owner(d.pop("owner", UNSET))


        def _parse_parent_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))


        def _parse_progress(data: object) -> Union['OperationProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = OperationProgress.from_dict(data)



                return progress_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationProgress', None, Unset], data)

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


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_status_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        def _parse_updated_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))


        operation_detail_response = cls(
            attempt=attempt,
            created_at=created_at,
            id=id,
            kind=kind,
            node_ids=node_ids,
            state=state,
            cancellation=cancellation,
            evidence_download=evidence_download,
            failure=failure,
            owner=owner,
            parent_id=parent_id,
            progress=progress,
            provenance=provenance,
            recovery=recovery,
            schema_version=schema_version,
            status_reason=status_reason,
            updated_at=updated_at,
        )

        return operation_detail_response
