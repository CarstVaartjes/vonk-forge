from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.error_context_response import ErrorContextResponse
  from ..models.http_failure_response import HttpFailureResponse





T = TypeVar("T", bound="BoundedErrorResponse")



@_attrs_define
class BoundedErrorResponse:
    """
        Attributes:
            detail (str):
            context (ErrorContextResponse | None | Unset):
            outcome (HttpFailureResponse | None | Unset):
     """

    detail: str
    context: ErrorContextResponse | None | Unset = UNSET
    outcome: HttpFailureResponse | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.error_context_response import ErrorContextResponse # noqa: PLC0415
        from ..models.http_failure_response import HttpFailureResponse # noqa: PLC0415
        detail = self.detail

        context: dict[str, Any] | None | Unset
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, ErrorContextResponse):
            context = self.context.to_dict()
        else:
            context = self.context

        outcome: dict[str, Any] | None | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        elif isinstance(self.outcome, HttpFailureResponse):
            outcome = self.outcome.to_dict()
        else:
            outcome = self.outcome


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
        })
        if context is not UNSET:
            field_dict["context"] = context
        if outcome is not UNSET:
            field_dict["outcome"] = outcome

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_context_response import ErrorContextResponse # noqa: PLC0415
        from ..models.http_failure_response import HttpFailureResponse # noqa: PLC0415
        d = dict(src_dict)
        detail = d.pop("detail")

        def _parse_context(data: object) -> ErrorContextResponse | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = ErrorContextResponse.from_dict(data)



                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ErrorContextResponse | None | Unset, data)

        context = _parse_context(d.pop("context", UNSET))


        def _parse_outcome(data: object) -> HttpFailureResponse | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                outcome_type_0 = HttpFailureResponse.from_dict(data)



                return outcome_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(HttpFailureResponse | None | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))


        bounded_error_response = cls(
            detail=detail,
            context=context,
            outcome=outcome,
        )

        return bounded_error_response
