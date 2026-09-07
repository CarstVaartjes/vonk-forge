from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="RunSwitchConflictResponse")



@_attrs_define
class RunSwitchConflictResponse:
    """ The conflict document returned by mutating Run/Switch routes.

        Attributes:
            detail (str):
            request_id (str):
            code (Union[Literal['run-switch.operation_conflict'], Unset]):  Default: 'run-switch.operation_conflict'.
     """

    detail: str
    request_id: str
    code: Union[Literal['run-switch.operation_conflict'], Unset] = 'run-switch.operation_conflict'





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        request_id = self.request_id

        code = self.code


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "request_id": request_id,
        })
        if code is not UNSET:
            field_dict["code"] = code

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        request_id = d.pop("request_id")

        code = cast(Union[Literal['run-switch.operation_conflict'], Unset] , d.pop("code", UNSET))
        if code != 'run-switch.operation_conflict' and not isinstance(code, Unset):
            raise ValueError(f"code must match const 'run-switch.operation_conflict', got '{code}'")

        run_switch_conflict_response = cls(
            detail=detail,
            request_id=request_id,
            code=code,
        )

        return run_switch_conflict_response
