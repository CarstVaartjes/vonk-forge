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






T = TypeVar("T", bound="AgentFailureResult")



@_attrs_define
class AgentFailureResult:
    """
        Attributes:
            diagnostic (Union[None, Unset, str]):
            error_code (Union[None, Unset, str]):
            helper_error_code (Union[None, Unset, str]):
            helper_exit_code (Union[None, Unset, int]):
            operation (Union[AgentOperation, None, Unset]):
            reason (Union[None, Unset, str]):
            recovery (Union[None, Unset, str]):
            stage (Union[None, Unset, str]):
            status (Union[Literal['failed'], None, Unset]):
            summary (Union[None, Unset, str]):
            uncertain (Union[None, Unset, bool]):
     """

    diagnostic: Union[None, Unset, str] = UNSET
    error_code: Union[None, Unset, str] = UNSET
    helper_error_code: Union[None, Unset, str] = UNSET
    helper_exit_code: Union[None, Unset, int] = UNSET
    operation: Union[AgentOperation, None, Unset] = UNSET
    reason: Union[None, Unset, str] = UNSET
    recovery: Union[None, Unset, str] = UNSET
    stage: Union[None, Unset, str] = UNSET
    status: Union[Literal['failed'], None, Unset] = UNSET
    summary: Union[None, Unset, str] = UNSET
    uncertain: Union[None, Unset, bool] = UNSET





    def to_dict(self) -> dict[str, Any]:
        diagnostic: Union[None, Unset, str]
        if isinstance(self.diagnostic, Unset):
            diagnostic = UNSET
        else:
            diagnostic = self.diagnostic

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
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if helper_error_code is not UNSET:
            field_dict["helper_error_code"] = helper_error_code
        if helper_exit_code is not UNSET:
            field_dict["helper_exit_code"] = helper_exit_code
        if operation is not UNSET:
            field_dict["operation"] = operation
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
        d = dict(src_dict)
        def _parse_diagnostic(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        diagnostic = _parse_diagnostic(d.pop("diagnostic", UNSET))


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
            error_code=error_code,
            helper_error_code=helper_error_code,
            helper_exit_code=helper_exit_code,
            operation=operation,
            reason=reason,
            recovery=recovery,
            stage=stage,
            status=status,
            summary=summary,
            uncertain=uncertain,
        )

        return agent_failure_result
