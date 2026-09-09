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
  from ..models.request_validation_issue import RequestValidationIssue
  from ..models.error_context_response import ErrorContextResponse





T = TypeVar("T", bound="RequestValidationProblem")



@_attrs_define
class RequestValidationProblem:
    """
        Attributes:
            detail (str):
            issues (list['RequestValidationIssue']):
            context (Union['ErrorContextResponse', None, Unset]):
     """

    detail: str
    issues: list['RequestValidationIssue']
    context: Union['ErrorContextResponse', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.request_validation_issue import RequestValidationIssue
        from ..models.error_context_response import ErrorContextResponse
        detail = self.detail

        issues = []
        for issues_item_data in self.issues:
            issues_item = issues_item_data.to_dict()
            issues.append(issues_item)



        context: Union[None, Unset, dict[str, Any]]
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, ErrorContextResponse):
            context = self.context.to_dict()
        else:
            context = self.context


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "issues": issues,
        })
        if context is not UNSET:
            field_dict["context"] = context

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.request_validation_issue import RequestValidationIssue
        from ..models.error_context_response import ErrorContextResponse
        d = dict(src_dict)
        detail = d.pop("detail")

        issues = []
        _issues = d.pop("issues")
        for issues_item_data in (_issues):
            issues_item = RequestValidationIssue.from_dict(issues_item_data)



            issues.append(issues_item)


        def _parse_context(data: object) -> Union['ErrorContextResponse', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = ErrorContextResponse.from_dict(data)



                return context_type_0
            except: # noqa: E722
                pass
            return cast(Union['ErrorContextResponse', None, Unset], data)

        context = _parse_context(d.pop("context", UNSET))


        request_validation_problem = cls(
            detail=detail,
            issues=issues,
            context=context,
        )

        return request_validation_problem
