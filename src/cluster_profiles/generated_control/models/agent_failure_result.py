from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_failure_kind import AgentFailureKind
from ..models.agent_failure_kind import check_agent_failure_kind
from ..models.agent_operation import AgentOperation
from ..models.agent_operation import check_agent_operation
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.failure_diagnostics import FailureDiagnostics
  from ..models.package_activation_receipt import PackageActivationReceipt





T = TypeVar("T", bound="AgentFailureResult")



@_attrs_define
class AgentFailureResult:
    """
        Attributes:
            diagnostic (None | str | Unset):
            diagnostics (FailureDiagnostics | None | Unset):
            error_code (None | str | Unset):
            failure_kind (AgentFailureKind | None | Unset):
            helper_error_code (None | str | Unset):
            helper_exit_code (int | None | Unset):
            operation (AgentOperation | None | Unset):
            package_activation (None | PackageActivationReceipt | Unset):
            reason (None | str | Unset):
            recovery (None | str | Unset):
            retry_after_seconds (int | None | Unset):
            stage (None | str | Unset):
            status (Literal['failed'] | None | Unset):
            summary (None | str | Unset):
            uncertain (bool | None | Unset):
     """

    diagnostic: None | str | Unset = UNSET
    diagnostics: FailureDiagnostics | None | Unset = UNSET
    error_code: None | str | Unset = UNSET
    failure_kind: AgentFailureKind | None | Unset = UNSET
    helper_error_code: None | str | Unset = UNSET
    helper_exit_code: int | None | Unset = UNSET
    operation: AgentOperation | None | Unset = UNSET
    package_activation: None | PackageActivationReceipt | Unset = UNSET
    reason: None | str | Unset = UNSET
    recovery: None | str | Unset = UNSET
    retry_after_seconds: int | None | Unset = UNSET
    stage: None | str | Unset = UNSET
    status: Literal['failed'] | None | Unset = UNSET
    summary: None | str | Unset = UNSET
    uncertain: bool | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        diagnostic: None | str | Unset
        if isinstance(self.diagnostic, Unset):
            diagnostic = UNSET
        else:
            diagnostic = self.diagnostic

        diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, FailureDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        failure_kind: None | str | Unset
        if isinstance(self.failure_kind, Unset):
            failure_kind = UNSET
        elif isinstance(self.failure_kind, str):
            failure_kind = self.failure_kind
        else:
            failure_kind = self.failure_kind

        helper_error_code: None | str | Unset
        if isinstance(self.helper_error_code, Unset):
            helper_error_code = UNSET
        else:
            helper_error_code = self.helper_error_code

        helper_exit_code: int | None | Unset
        if isinstance(self.helper_exit_code, Unset):
            helper_exit_code = UNSET
        else:
            helper_exit_code = self.helper_exit_code

        operation: None | str | Unset
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, str):
            operation = self.operation
        else:
            operation = self.operation

        package_activation: dict[str, Any] | None | Unset
        if isinstance(self.package_activation, Unset):
            package_activation = UNSET
        elif isinstance(self.package_activation, PackageActivationReceipt):
            package_activation = self.package_activation.to_dict()
        else:
            package_activation = self.package_activation

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        recovery: None | str | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        else:
            recovery = self.recovery

        retry_after_seconds: int | None | Unset
        if isinstance(self.retry_after_seconds, Unset):
            retry_after_seconds = UNSET
        else:
            retry_after_seconds = self.retry_after_seconds

        stage: None | str | Unset
        if isinstance(self.stage, Unset):
            stage = UNSET
        else:
            stage = self.stage

        status: Literal['failed'] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        uncertain: bool | None | Unset
        if isinstance(self.uncertain, Unset):
            uncertain = UNSET
        else:
            uncertain = self.uncertain


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if diagnostic is not UNSET:
            field_dict["diagnostic"] = diagnostic
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if failure_kind is not UNSET:
            field_dict["failure_kind"] = failure_kind
        if helper_error_code is not UNSET:
            field_dict["helper_error_code"] = helper_error_code
        if helper_exit_code is not UNSET:
            field_dict["helper_exit_code"] = helper_exit_code
        if operation is not UNSET:
            field_dict["operation"] = operation
        if package_activation is not UNSET:
            field_dict["package_activation"] = package_activation
        if reason is not UNSET:
            field_dict["reason"] = reason
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if retry_after_seconds is not UNSET:
            field_dict["retry_after_seconds"] = retry_after_seconds
        if stage is not UNSET:
            field_dict["stage"] = stage
        if status is not UNSET:
            field_dict["status"] = status
        if summary is not UNSET:
            field_dict["summary"] = summary
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        d = dict(src_dict)
        def _parse_diagnostic(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        diagnostic = _parse_diagnostic(d.pop("diagnostic", UNSET))


        def _parse_diagnostics(data: object) -> FailureDiagnostics | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FailureDiagnostics | None | Unset, data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))


        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))


        def _parse_failure_kind(data: object) -> AgentFailureKind | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                failure_kind_type_0 = check_agent_failure_kind(data)



                return failure_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentFailureKind | None | Unset, data)

        failure_kind = _parse_failure_kind(d.pop("failure_kind", UNSET))


        def _parse_helper_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        helper_error_code = _parse_helper_error_code(d.pop("helper_error_code", UNSET))


        def _parse_helper_exit_code(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        helper_exit_code = _parse_helper_exit_code(d.pop("helper_exit_code", UNSET))


        def _parse_operation(data: object) -> AgentOperation | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                operation_type_0 = check_agent_operation(data)



                return operation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentOperation | None | Unset, data)

        operation = _parse_operation(d.pop("operation", UNSET))


        def _parse_package_activation(data: object) -> None | PackageActivationReceipt | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_activation_type_0 = PackageActivationReceipt.from_dict(data)



                return package_activation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PackageActivationReceipt | Unset, data)

        package_activation = _parse_package_activation(d.pop("package_activation", UNSET))


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_recovery(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        def _parse_retry_after_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        retry_after_seconds = _parse_retry_after_seconds(d.pop("retry_after_seconds", UNSET))


        def _parse_stage(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stage = _parse_stage(d.pop("stage", UNSET))


        def _parse_status(data: object) -> Literal['failed'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            status_type_0 = cast(Literal['failed'] , data)
            if status_type_0 != 'failed':
                raise ValueError(f"status_type_0 must match const 'failed', got '{status_type_0}'")
            return status_type_0
            return cast(Literal['failed'] | None | Unset, data)

        status = _parse_status(d.pop("status", UNSET))


        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))


        def _parse_uncertain(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        uncertain = _parse_uncertain(d.pop("uncertain", UNSET))


        agent_failure_result = cls(
            diagnostic=diagnostic,
            diagnostics=diagnostics,
            error_code=error_code,
            failure_kind=failure_kind,
            helper_error_code=helper_error_code,
            helper_exit_code=helper_exit_code,
            operation=operation,
            package_activation=package_activation,
            reason=reason,
            recovery=recovery,
            retry_after_seconds=retry_after_seconds,
            stage=stage,
            status=status,
            summary=summary,
            uncertain=uncertain,
        )

        return agent_failure_result
