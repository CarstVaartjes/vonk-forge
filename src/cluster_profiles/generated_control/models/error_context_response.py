from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.error_context_response_decision import check_error_context_response_decision
from ..models.error_context_response_decision import ErrorContextResponseDecision
from ..models.error_context_response_source import check_error_context_response_source
from ..models.error_context_response_source import ErrorContextResponseSource
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ErrorContextResponse")



@_attrs_define
class ErrorContextResponse:
    """ Safe context shared by public errors and generated clients.

        Attributes:
            code (str):
            decision (ErrorContextResponseDecision):
            operation (str):
            source (ErrorContextResponseSource):
            endpoint (None | str | Unset):
            http_status (int | None | Unset):
            request_id (None | str | Unset):
            retryable (bool | Unset):  Default: False.
     """

    code: str
    decision: ErrorContextResponseDecision
    operation: str
    source: ErrorContextResponseSource
    endpoint: None | str | Unset = UNSET
    http_status: int | None | Unset = UNSET
    request_id: None | str | Unset = UNSET
    retryable: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        decision: str = self.decision

        operation = self.operation

        source: str = self.source

        endpoint: None | str | Unset
        if isinstance(self.endpoint, Unset):
            endpoint = UNSET
        else:
            endpoint = self.endpoint

        http_status: int | None | Unset
        if isinstance(self.http_status, Unset):
            http_status = UNSET
        else:
            http_status = self.http_status

        request_id: None | str | Unset
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id

        retryable = self.retryable


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "decision": decision,
            "operation": operation,
            "source": source,
        })
        if endpoint is not UNSET:
            field_dict["endpoint"] = endpoint
        if http_status is not UNSET:
            field_dict["http_status"] = http_status
        if request_id is not UNSET:
            field_dict["request_id"] = request_id
        if retryable is not UNSET:
            field_dict["retryable"] = retryable

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        decision = check_error_context_response_decision(d.pop("decision"))




        operation = d.pop("operation")

        source = check_error_context_response_source(d.pop("source"))




        def _parse_endpoint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        endpoint = _parse_endpoint(d.pop("endpoint", UNSET))


        def _parse_http_status(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        http_status = _parse_http_status(d.pop("http_status", UNSET))


        def _parse_request_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))


        retryable = d.pop("retryable", UNSET)

        error_context_response = cls(
            code=code,
            decision=decision,
            operation=operation,
            source=source,
            endpoint=endpoint,
            http_status=http_status,
            request_id=request_id,
            retryable=retryable,
        )

        return error_context_response
