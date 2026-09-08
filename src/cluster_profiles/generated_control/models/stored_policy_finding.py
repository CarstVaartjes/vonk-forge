from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="StoredPolicyFinding")



@_attrs_define
class StoredPolicyFinding:
    """
        Attributes:
            code (str):
            detail (str):
            line (Union[None, int]):
            path (str):
     """

    code: str
    detail: str
    line: Union[None, int]
    path: str





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        line: Union[None, int]
        line = self.line

        path = self.path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "line": line,
            "path": path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        def _parse_line(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        line = _parse_line(d.pop("line"))


        path = d.pop("path")

        stored_policy_finding = cls(
            code=code,
            detail=detail,
            line=line,
            path=path,
        )

        return stored_policy_finding
