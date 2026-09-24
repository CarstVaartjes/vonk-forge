from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeBuildPolicyFinding")



@_attrs_define
class RecipeBuildPolicyFinding:
    """
        Attributes:
            code (str):
            detail (str):
            path (str):
            line (Union[None, Unset, int]):
     """

    code: str
    detail: str
    path: str
    line: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        path = self.path

        line: Union[None, Unset, int]
        if isinstance(self.line, Unset):
            line = UNSET
        else:
            line = self.line


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "path": path,
        })
        if line is not UNSET:
            field_dict["line"] = line

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        path = d.pop("path")

        def _parse_line(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        line = _parse_line(d.pop("line", UNSET))


        recipe_build_policy_finding = cls(
            code=code,
            detail=detail,
            path=path,
            line=line,
        )

        return recipe_build_policy_finding
