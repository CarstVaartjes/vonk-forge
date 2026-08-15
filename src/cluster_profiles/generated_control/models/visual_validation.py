from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VisualValidation")



@_attrs_define
class VisualValidation:
    """
        Attributes:
            benchmark_count (int):
            checks (list[str]):
     """

    benchmark_count: int
    checks: list[str]





    def to_dict(self) -> dict[str, Any]:
        benchmark_count = self.benchmark_count

        checks = self.checks




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "benchmark_count": benchmark_count,
            "checks": checks,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        benchmark_count = d.pop("benchmark_count")

        checks = cast(list[str], d.pop("checks"))


        visual_validation = cls(
            benchmark_count=benchmark_count,
            checks=checks,
        )

        return visual_validation
