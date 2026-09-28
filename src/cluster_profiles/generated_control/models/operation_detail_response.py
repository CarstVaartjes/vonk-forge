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
  from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
  from ..models.operation_evidence_download import OperationEvidenceDownload
  from ..models.operation_failure_evidence import OperationFailureEvidence
  from ..models.operation_owner_reference import OperationOwnerReference
  from ..models.operation_progress import OperationProgress
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
            cancellation (FleetProfileApplicationCancellationView | None | Unset):
            evidence_download (None | OperationEvidenceDownload | Unset):
            failure (AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset):
            owner (None | OperationOwnerReference | Unset):
            parent_id (None | str | Unset):
            progress (None | OperationProgress | Unset):
            recovery (None | OperationRecovery | Unset):
            status_reason (None | str | Unset):
            updated_at (None | str | Unset):
     """

    attempt: int
    created_at: str
    id: str
    kind: str
    node_ids: list[str]
    state: str
    cancellation: FleetProfileApplicationCancellationView | None | Unset = UNSET
    evidence_download: None | OperationEvidenceDownload | Unset = UNSET
    failure: AgentFailureResult | AvailabilityOperationFailure | None | OperationFailureEvidence | Unset = UNSET
    owner: None | OperationOwnerReference | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    progress: None | OperationProgress | Unset = UNSET
    recovery: None | OperationRecovery | Unset = UNSET
    status_reason: None | str | Unset = UNSET
    updated_at: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_failure_result import AgentFailureResult # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.operation_evidence_download import OperationEvidenceDownload # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        from ..models.operation_owner_reference import OperationOwnerReference # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        attempt = self.attempt

        created_at = self.created_at

        id = self.id

        kind = self.kind

        node_ids = self.node_ids



        state = self.state

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationView):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

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

        owner: dict[str, Any] | None | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        elif isinstance(self.owner, OperationOwnerReference):
            owner = self.owner.to_dict()
        else:
            owner = self.owner

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

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

        status_reason: None | str | Unset
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason

        updated_at: None | str | Unset
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
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_failure_result import AgentFailureResult # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.operation_evidence_download import OperationEvidenceDownload # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        from ..models.operation_owner_reference import OperationOwnerReference # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        d = dict(src_dict)
        attempt = d.pop("attempt")

        created_at = d.pop("created_at")

        id = d.pop("id")

        kind = d.pop("kind")

        node_ids = cast(list[str], d.pop("node_ids"))


        state = d.pop("state")

        def _parse_cancellation(data: object) -> FleetProfileApplicationCancellationView | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = FleetProfileApplicationCancellationView.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileApplicationCancellationView | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


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


        def _parse_owner(data: object) -> None | OperationOwnerReference | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                owner_type_0 = OperationOwnerReference.from_dict(data)



                return owner_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationOwnerReference | Unset, data)

        owner = _parse_owner(d.pop("owner", UNSET))


        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))


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


        def _parse_status_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        def _parse_updated_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
            recovery=recovery,
            status_reason=status_reason,
            updated_at=updated_at,
        )

        return operation_detail_response
