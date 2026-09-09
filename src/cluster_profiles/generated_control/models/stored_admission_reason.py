from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="StoredAdmissionReason")



@_attrs_define
class StoredAdmissionReason:
    """
        Attributes:
            code (str):
            detail (str):
     """

    code: str
    detail: str





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        stored_admission_reason = cls(
            code=code,
            detail=detail,
        )

        return stored_admission_reason
