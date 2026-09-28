from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelFormat")



@_attrs_define
class ModelFormat:
    """
        Attributes:
            precision (str):
            quantization (str):
     """

    precision: str
    quantization: str





    def to_dict(self) -> dict[str, Any]:
        precision = self.precision

        quantization = self.quantization


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "precision": precision,
            "quantization": quantization,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        precision = d.pop("precision")

        quantization = d.pop("quantization")

        model_format = cls(
            precision=precision,
            quantization=quantization,
        )

        return model_format
