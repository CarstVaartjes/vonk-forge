from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="LibraryFacetValues")



@_attrs_define
class LibraryFacetValues:
    """
        Attributes:
            family (list[str]):
            quantization (list[str]):
            usage (list[str]):
            version (list[str]):
     """

    family: list[str]
    quantization: list[str]
    usage: list[str]
    version: list[str]





    def to_dict(self) -> dict[str, Any]:
        family = self.family



        quantization = self.quantization



        usage = self.usage



        version = self.version




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "quantization": quantization,
            "usage": usage,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        family = cast(list[str], d.pop("family"))


        quantization = cast(list[str], d.pop("quantization"))


        usage = cast(list[str], d.pop("usage"))


        version = cast(list[str], d.pop("version"))


        library_facet_values = cls(
            family=family,
            quantization=quantization,
            usage=usage,
            version=version,
        )

        return library_facet_values
