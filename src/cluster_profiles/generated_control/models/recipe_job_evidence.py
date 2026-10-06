from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeJobEvidence")



@_attrs_define
class RecipeJobEvidence:
    """
        Attributes:
            elapsed_milliseconds (int):
            peak_memory_bytes (int | None):
     """

    elapsed_milliseconds: int
    peak_memory_bytes: int | None





    def to_dict(self) -> dict[str, Any]:
        elapsed_milliseconds = self.elapsed_milliseconds

        peak_memory_bytes: int | None
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

        def _parse_peak_memory_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        peak_memory_bytes = _parse_peak_memory_bytes(d.pop("peak_memory_bytes"))


        recipe_job_evidence = cls(
            elapsed_milliseconds=elapsed_milliseconds,
            peak_memory_bytes=peak_memory_bytes,
        )

        return recipe_job_evidence
