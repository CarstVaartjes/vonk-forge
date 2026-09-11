from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="LibraryFacetValues")



@_attrs_define
class LibraryFacetValues:
    """
        Attributes:
            family (list[str]):
            quantization (list[str]):
            usage (list[str]):
            version (list[str]):
            alignment (Union[Unset, list[str]]):
            publisher (Union[Unset, list[str]]):
            sparks (Union[Unset, list[int]]):
     """

    family: list[str]
    quantization: list[str]
    usage: list[str]
    version: list[str]
    alignment: Union[Unset, list[str]] = UNSET
    publisher: Union[Unset, list[str]] = UNSET
    sparks: Union[Unset, list[int]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        family = self.family



        quantization = self.quantization



        usage = self.usage



        version = self.version



        alignment: Union[Unset, list[str]] = UNSET
        if not isinstance(self.alignment, Unset):
            alignment = self.alignment



        publisher: Union[Unset, list[str]] = UNSET
        if not isinstance(self.publisher, Unset):
            publisher = self.publisher



        sparks: Union[Unset, list[int]] = UNSET
        if not isinstance(self.sparks, Unset):
            sparks = self.sparks




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "quantization": quantization,
            "usage": usage,
            "version": version,
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment
        if publisher is not UNSET:
            field_dict["publisher"] = publisher
        if sparks is not UNSET:
            field_dict["sparks"] = sparks

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        family = cast(list[str], d.pop("family"))


        quantization = cast(list[str], d.pop("quantization"))


        usage = cast(list[str], d.pop("usage"))


        version = cast(list[str], d.pop("version"))


        alignment = cast(list[str], d.pop("alignment", UNSET))


        publisher = cast(list[str], d.pop("publisher", UNSET))


        sparks = cast(list[int], d.pop("sparks", UNSET))


        library_facet_values = cls(
            family=family,
            quantization=quantization,
            usage=usage,
            version=version,
            alignment=alignment,
            publisher=publisher,
            sparks=sparks,
        )

        return library_facet_values
