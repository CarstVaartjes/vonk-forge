from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.request_validation_issue import RequestValidationIssue





T = TypeVar("T", bound="RequestValidationProblem")



@_attrs_define
class RequestValidationProblem:
    """
        Attributes:
            detail (str):
            issues (list['RequestValidationIssue']):
     """

    detail: str
    issues: list['RequestValidationIssue']





    def to_dict(self) -> dict[str, Any]:
        from ..models.request_validation_issue import RequestValidationIssue
        detail = self.detail

        issues = []
        for issues_item_data in self.issues:
            issues_item = issues_item_data.to_dict()
            issues.append(issues_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "issues": issues,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.request_validation_issue import RequestValidationIssue
        d = dict(src_dict)
        detail = d.pop("detail")

        issues = []
        _issues = d.pop("issues")
        for issues_item_data in (_issues):
            issues_item = RequestValidationIssue.from_dict(issues_item_data)



            issues.append(issues_item)


        request_validation_problem = cls(
            detail=detail,
            issues=issues,
        )

        return request_validation_problem
