from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LifecycleCodeFailureResult")



@_attrs_define
class LifecycleCodeFailureResult:
    """ Bounded failure marker used by the Controller's node projector.

        Attributes:
            code (str):
            detail (Union[None, Unset, str]):
     """

    code: str
    detail: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail: Union[None, Unset, str]
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        def _parse_detail(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        detail = _parse_detail(d.pop("detail", UNSET))


        lifecycle_code_failure_result = cls(
            code=code,
            detail=detail,
        )

        return lifecycle_code_failure_result
