from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="RecipeJobEvidence")



@_attrs_define
class RecipeJobEvidence:
    """
        Attributes:
            elapsed_milliseconds (int):
            peak_memory_bytes (Union[None, int]):
     """

    elapsed_milliseconds: int
    peak_memory_bytes: Union[None, int]





    def to_dict(self) -> dict[str, Any]:
        elapsed_milliseconds = self.elapsed_milliseconds

        peak_memory_bytes: Union[None, int]
        peak_memory_bytes = self.peak_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "elapsed_milliseconds": elapsed_milliseconds,
            "peak_memory_bytes": peak_memory_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        elapsed_milliseconds = d.pop("elapsed_milliseconds")

        def _parse_peak_memory_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        peak_memory_bytes = _parse_peak_memory_bytes(d.pop("peak_memory_bytes"))


        recipe_job_evidence = cls(
            elapsed_milliseconds=elapsed_milliseconds,
            peak_memory_bytes=peak_memory_bytes,
        )

        return recipe_job_evidence
