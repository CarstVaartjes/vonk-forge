from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="OperationEvidenceDownload")



@_attrs_define
class OperationEvidenceDownload:
    """ Where this failed attempt's diagnostics render on request.

        Attributes:
            href (str):
     """

    href: str





    def to_dict(self) -> dict[str, Any]:
        href = self.href


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "href": href,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        href = d.pop("href")

        operation_evidence_download = cls(
            href=href,
        )

        return operation_evidence_download
