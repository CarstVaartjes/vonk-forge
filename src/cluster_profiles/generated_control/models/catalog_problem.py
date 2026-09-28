from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CatalogProblem")



@_attrs_define
class CatalogProblem:
    """
        Attributes:
            code (str):
            detail (str):
            request_id (str):
     """

    code: str
    detail: str
    request_id: str





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        request_id = self.request_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "request_id": request_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        request_id = d.pop("request_id")

        catalog_problem = cls(
            code=code,
            detail=detail,
            request_id=request_id,
        )

        return catalog_problem
