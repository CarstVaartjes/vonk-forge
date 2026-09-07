from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationFailureEvidence")



@_attrs_define
class OperationFailureEvidence:
    """ Small, sanitized operator evidence safe to expose in status responses.

        Attributes:
            error_code (str):
            summary (str):
            detail (Union[None, Unset, str]):
            retryable (Union[Unset, bool]):  Default: False.
            uncertain (Union[Unset, bool]):  Default: False.
     """

    error_code: str
    summary: str
    detail: Union[None, Unset, str] = UNSET
    retryable: Union[Unset, bool] = False
    uncertain: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        error_code = self.error_code

        summary = self.summary

        detail: Union[None, Unset, str]
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        retryable = self.retryable

        uncertain = self.uncertain


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "error_code": error_code,
            "summary": summary,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail
        if retryable is not UNSET:
            field_dict["retryable"] = retryable
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        error_code = d.pop("error_code")

        summary = d.pop("summary")

        def _parse_detail(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        detail = _parse_detail(d.pop("detail", UNSET))


        retryable = d.pop("retryable", UNSET)

        uncertain = d.pop("uncertain", UNSET)

        operation_failure_evidence = cls(
            error_code=error_code,
            summary=summary,
            detail=detail,
            retryable=retryable,
            uncertain=uncertain,
        )

        return operation_failure_evidence
