from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_operation import AgentOperation
from ..models.agent_operation import check_agent_operation
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.package_activation_receipt import PackageActivationReceipt
  from ..models.failure_diagnostics import FailureDiagnostics





T = TypeVar("T", bound="AgentFailureResult")



@_attrs_define
class AgentFailureResult:
    """
        Attributes:
            diagnostic (Union[None, Unset, str]):
            diagnostics (Union['FailureDiagnostics', None, Unset]):
            error_code (Union[None, Unset, str]):
            helper_error_code (Union[None, Unset, str]):
            helper_exit_code (Union[None, Unset, int]):
            operation (Union[AgentOperation, None, Unset]):
            package_activation (Union['PackageActivationReceipt', None, Unset]):
            reason (Union[None, Unset, str]):
            recovery (Union[None, Unset, str]):
            stage (Union[None, Unset, str]):
            status (Union[Literal['failed'], None, Unset]):
            summary (Union[None, Unset, str]):
            uncertain (Union[None, Unset, bool]):
     """

    diagnostic: Union[None, Unset, str] = UNSET
    diagnostics: Union['FailureDiagnostics', None, Unset] = UNSET
    error_code: Union[None, Unset, str] = UNSET
    helper_error_code: Union[None, Unset, str] = UNSET
    helper_exit_code: Union[None, Unset, int] = UNSET
    operation: Union[AgentOperation, None, Unset] = UNSET
    package_activation: Union['PackageActivationReceipt', None, Unset] = UNSET
    reason: Union[None, Unset, str] = UNSET
    recovery: Union[None, Unset, str] = UNSET
    stage: Union[None, Unset, str] = UNSET
    status: Union[Literal['failed'], None, Unset] = UNSET
    summary: Union[None, Unset, str] = UNSET
    uncertain: Union[None, Unset, bool] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_activation_receipt import PackageActivationReceipt
        from ..models.failure_diagnostics import FailureDiagnostics
        diagnostic: Union[None, Unset, str]
        if isinstance(self.diagnostic, Unset):
            diagnostic = UNSET
        else:
            diagnostic = self.diagnostic

        diagnostics: Union[None, Unset, dict[str, Any]]
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, FailureDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        error_code: Union[None, Unset, str]
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        helper_error_code: Union[None, Unset, str]
        if isinstance(self.helper_error_code, Unset):
            helper_error_code = UNSET
        else:
            helper_error_code = self.helper_error_code

        helper_exit_code: Union[None, Unset, int]
        if isinstance(self.helper_exit_code, Unset):
            helper_exit_code = UNSET
        else:
            helper_exit_code = self.helper_exit_code

        operation: Union[None, Unset, str]
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, str):
            operation = self.operation
        else:
            operation = self.operation

        package_activation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.package_activation, Unset):
            package_activation = UNSET
        elif isinstance(self.package_activation, PackageActivationReceipt):
            package_activation = self.package_activation.to_dict()
        else:
            package_activation = self.package_activation

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        recovery: Union[None, Unset, str]
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        else:
            recovery = self.recovery

        stage: Union[None, Unset, str]
        if isinstance(self.stage, Unset):
            stage = UNSET
        else:
            stage = self.stage

        status: Union[Literal['failed'], None, Unset]
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        summary: Union[None, Unset, str]
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        uncertain: Union[None, Unset, bool]
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
        from ..models.package_activation_receipt import PackageActivationReceipt
        from ..models.failure_diagnostics import FailureDiagnostics
        d = dict(src_dict)
        def _parse_diagnostic(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        diagnostic = _parse_diagnostic(d.pop("diagnostic", UNSET))


        def _parse_diagnostics(data: object) -> Union['FailureDiagnostics', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return diagnostics_type_0
            except: # noqa: E722
                pass
            return cast(Union['FailureDiagnostics', None, Unset], data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))


        def _parse_error_code(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))


        def _parse_helper_error_code(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        helper_error_code = _parse_helper_error_code(d.pop("helper_error_code", UNSET))


        def _parse_helper_exit_code(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        helper_exit_code = _parse_helper_exit_code(d.pop("helper_exit_code", UNSET))


        def _parse_operation(data: object) -> Union[AgentOperation, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                operation_type_0 = check_agent_operation(data)



                return operation_type_0
            except: # noqa: E722
                pass
            return cast(Union[AgentOperation, None, Unset], data)

        operation = _parse_operation(d.pop("operation", UNSET))


        def _parse_package_activation(data: object) -> Union['PackageActivationReceipt', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_activation_type_0 = PackageActivationReceipt.from_dict(data)



                return package_activation_type_0
            except: # noqa: E722
                pass
            return cast(Union['PackageActivationReceipt', None, Unset], data)

        package_activation = _parse_package_activation(d.pop("package_activation", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_recovery(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        def _parse_stage(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        stage = _parse_stage(d.pop("stage", UNSET))


        def _parse_status(data: object) -> Union[Literal['failed'], None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            status_type_0 = cast(Literal['failed'] , data)
            if status_type_0 != 'failed':
                raise ValueError(f"status_type_0 must match const 'failed', got '{status_type_0}'")
            return status_type_0
            return cast(Union[Literal['failed'], None, Unset], data)

        status = _parse_status(d.pop("status", UNSET))


        def _parse_summary(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        summary = _parse_summary(d.pop("summary", UNSET))


        def _parse_uncertain(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        uncertain = _parse_uncertain(d.pop("uncertain", UNSET))


        agent_failure_result = cls(
            diagnostic=diagnostic,
            diagnostics=diagnostics,
            error_code=error_code,
            helper_error_code=helper_error_code,
            helper_exit_code=helper_exit_code,
            operation=operation,
            package_activation=package_activation,
            reason=reason,
            recovery=recovery,
            stage=stage,
            status=status,
            summary=summary,
            uncertain=uncertain,
        )

        return agent_failure_result
