from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_diagnostics_response import AgentUpgradeDiagnosticsResponse
  from ..models.job_operation_response import JobOperationResponse
  from ..models.job_progress import JobProgress
  from ..models.operation_recovery import OperationRecovery





T = TypeVar("T", bound="JobDetailResponse")



@_attrs_define
class JobDetailResponse:
    """
        Attributes:
            authority_revision (str):
            current_attempt (int):
            id (str):
            kind (str):
            operation_total (int | None):
            operations (list[JobOperationResponse] | None):
            progress (JobProgress | None):
            state (str):
            target_total (int):
            targets (list[str]):
            agent_upgrade_diagnostics (AgentUpgradeDiagnosticsResponse | None | Unset):
            operation_next_cursor (None | str | Unset):
            projection_issue (None | str | Unset):
            recovery (None | OperationRecovery | Unset):
            status_reason (None | str | Unset):
            target_next_cursor (None | str | Unset):
     """

    authority_revision: str
    current_attempt: int
    id: str
    kind: str
    operation_total: int | None
    operations: list[JobOperationResponse] | None
    progress: JobProgress | None
    state: str
    target_total: int
    targets: list[str]
    agent_upgrade_diagnostics: AgentUpgradeDiagnosticsResponse | None | Unset = UNSET
    operation_next_cursor: None | str | Unset = UNSET
    projection_issue: None | str | Unset = UNSET
    recovery: None | OperationRecovery | Unset = UNSET
    status_reason: None | str | Unset = UNSET
    target_next_cursor: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_diagnostics_response import AgentUpgradeDiagnosticsResponse # noqa: PLC0415
        from ..models.job_operation_response import JobOperationResponse # noqa: PLC0415
        from ..models.job_progress import JobProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        authority_revision = self.authority_revision

        current_attempt = self.current_attempt

        id = self.id

        kind = self.kind

        operation_total: int | None
        operation_total = self.operation_total

        operations: list[dict[str, Any]] | None
        if isinstance(self.operations, list):
            operations = []
            for operations_type_0_item_data in self.operations:
                operations_type_0_item = operations_type_0_item_data.to_dict()
                operations.append(operations_type_0_item)


        else:
            operations = self.operations

        progress: dict[str, Any] | None
        if isinstance(self.progress, JobProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        state = self.state

        target_total = self.target_total

        targets = self.targets



        agent_upgrade_diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.agent_upgrade_diagnostics, Unset):
            agent_upgrade_diagnostics = UNSET
        elif isinstance(self.agent_upgrade_diagnostics, AgentUpgradeDiagnosticsResponse):
            agent_upgrade_diagnostics = self.agent_upgrade_diagnostics.to_dict()
        else:
            agent_upgrade_diagnostics = self.agent_upgrade_diagnostics

        operation_next_cursor: None | str | Unset
        if isinstance(self.operation_next_cursor, Unset):
            operation_next_cursor = UNSET
        else:
            operation_next_cursor = self.operation_next_cursor

        projection_issue: None | str | Unset
        if isinstance(self.projection_issue, Unset):
            projection_issue = UNSET
        else:
            projection_issue = self.projection_issue

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

        target_next_cursor: None | str | Unset
        if isinstance(self.target_next_cursor, Unset):
            target_next_cursor = UNSET
        else:
            target_next_cursor = self.target_next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "current_attempt": current_attempt,
            "id": id,
            "kind": kind,
            "operation_total": operation_total,
            "operations": operations,
            "progress": progress,
            "state": state,
            "target_total": target_total,
            "targets": targets,
        })
        if agent_upgrade_diagnostics is not UNSET:
            field_dict["agent_upgrade_diagnostics"] = agent_upgrade_diagnostics
        if operation_next_cursor is not UNSET:
            field_dict["operation_next_cursor"] = operation_next_cursor
        if projection_issue is not UNSET:
            field_dict["projection_issue"] = projection_issue
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason
        if target_next_cursor is not UNSET:
            field_dict["target_next_cursor"] = target_next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_diagnostics_response import AgentUpgradeDiagnosticsResponse # noqa: PLC0415
        from ..models.job_operation_response import JobOperationResponse # noqa: PLC0415
        from ..models.job_progress import JobProgress # noqa: PLC0415
        from ..models.operation_recovery import OperationRecovery # noqa: PLC0415
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        current_attempt = d.pop("current_attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        def _parse_operation_total(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        operation_total = _parse_operation_total(d.pop("operation_total"))


        def _parse_operations(data: object) -> list[JobOperationResponse] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                operations_type_0 = []
                _operations_type_0 = data
                for operations_type_0_item_data in (_operations_type_0):
                    operations_type_0_item = JobOperationResponse.from_dict(operations_type_0_item_data)



                    operations_type_0.append(operations_type_0_item)

                return operations_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[JobOperationResponse] | None, data)

        operations = _parse_operations(d.pop("operations"))


        def _parse_progress(data: object) -> JobProgress | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = JobProgress.from_dict(data)



                return progress_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobProgress | None, data)

        progress = _parse_progress(d.pop("progress"))


        state = d.pop("state")

        target_total = d.pop("target_total")

        targets = cast(list[str], d.pop("targets"))


        def _parse_agent_upgrade_diagnostics(data: object) -> AgentUpgradeDiagnosticsResponse | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                agent_upgrade_diagnostics_type_0 = AgentUpgradeDiagnosticsResponse.from_dict(data)



                return agent_upgrade_diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentUpgradeDiagnosticsResponse | None | Unset, data)

        agent_upgrade_diagnostics = _parse_agent_upgrade_diagnostics(d.pop("agent_upgrade_diagnostics", UNSET))


        def _parse_operation_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_next_cursor = _parse_operation_next_cursor(d.pop("operation_next_cursor", UNSET))


        def _parse_projection_issue(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        projection_issue = _parse_projection_issue(d.pop("projection_issue", UNSET))


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


        def _parse_target_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_next_cursor = _parse_target_next_cursor(d.pop("target_next_cursor", UNSET))


        job_detail_response = cls(
            authority_revision=authority_revision,
            current_attempt=current_attempt,
            id=id,
            kind=kind,
            operation_total=operation_total,
            operations=operations,
            progress=progress,
            state=state,
            target_total=target_total,
            targets=targets,
            agent_upgrade_diagnostics=agent_upgrade_diagnostics,
            operation_next_cursor=operation_next_cursor,
            projection_issue=projection_issue,
            recovery=recovery,
            status_reason=status_reason,
            target_next_cursor=target_next_cursor,
        )

        return job_detail_response
