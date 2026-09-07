from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_format_container import check_model_format_container
from ..models.model_format_container import ModelFormatContainer
from typing import cast






T = TypeVar("T", bound="ModelFormat")



@_attrs_define
class ModelFormat:
    """
        Attributes:
            container (ModelFormatContainer):
            precision (str):
            quantization (str):
     """

    container: ModelFormatContainer
    precision: str
    quantization: str





    def to_dict(self) -> dict[str, Any]:
        container: str = self.container

        precision = self.precision

        quantization = self.quantization


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "container": container,
            "precision": precision,
            "quantization": quantization,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        container = check_model_format_container(d.pop("container"))




        precision = d.pop("precision")

        quantization = d.pop("quantization")

        model_format = cls(
            container=container,
            precision=precision,
            quantization=quantization,
        )

        return model_format
