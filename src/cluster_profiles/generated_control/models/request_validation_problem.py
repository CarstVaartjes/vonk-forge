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
  from ..models.request_validation_issue import RequestValidationIssue





T = TypeVar("T", bound="RequestValidationProblem")



@_attrs_define
class RequestValidationProblem:
    """
        Attributes:
            detail (str):
            issues (list[RequestValidationIssue]):
            candidates (list[str] | None | Unset):
            context (ErrorContextResponse | None | Unset):
            outcome (HttpFailureResponse | None | Unset):
     """

    detail: str
    issues: list[RequestValidationIssue]
    candidates: list[str] | None | Unset = UNSET
    context: ErrorContextResponse | None | Unset = UNSET
    outcome: HttpFailureResponse | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.error_context_response import ErrorContextResponse # noqa: PLC0415
        from ..models.http_failure_response import HttpFailureResponse # noqa: PLC0415
        from ..models.request_validation_issue import RequestValidationIssue # noqa: PLC0415
        detail = self.detail

        issues = []
        for issues_item_data in self.issues:
            issues_item = issues_item_data.to_dict()
            issues.append(issues_item)



        candidates: list[str] | None | Unset
        if isinstance(self.candidates, Unset):
            candidates = UNSET
        elif isinstance(self.candidates, list):
            candidates = self.candidates


        else:
            candidates = self.candidates

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
            "issues": issues,
        })
        if candidates is not UNSET:
            field_dict["candidates"] = candidates
        if context is not UNSET:
            field_dict["context"] = context
        if outcome is not UNSET:
            field_dict["outcome"] = outcome

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_context_response import ErrorContextResponse # noqa: PLC0415
        from ..models.http_failure_response import HttpFailureResponse # noqa: PLC0415
        from ..models.request_validation_issue import RequestValidationIssue # noqa: PLC0415
        d = dict(src_dict)
        detail = d.pop("detail")

        issues = []
        _issues = d.pop("issues")
        for issues_item_data in (_issues):
            issues_item = RequestValidationIssue.from_dict(issues_item_data)



            issues.append(issues_item)


        def _parse_candidates(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                candidates_type_0 = cast(list[str], data)

                return candidates_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        candidates = _parse_candidates(d.pop("candidates", UNSET))


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


        request_validation_problem = cls(
            detail=detail,
            issues=issues,
            candidates=candidates,
            context=context,
            outcome=outcome,
        )

        return request_validation_problem
