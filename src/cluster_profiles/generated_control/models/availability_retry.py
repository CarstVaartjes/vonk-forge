from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="AvailabilityRetry")



@_attrs_define
class AvailabilityRetry:
    """
        Attributes:
            automatic_attempts (int):
            operator_retries (int):
     """

    automatic_attempts: int
    operator_retries: int





    def to_dict(self) -> dict[str, Any]:
        automatic_attempts = self.automatic_attempts

        operator_retries = self.operator_retries


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "automatic_attempts": automatic_attempts,
            "operator_retries": operator_retries,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        automatic_attempts = d.pop("automatic_attempts")

        operator_retries = d.pop("operator_retries")

        availability_retry = cls(
            automatic_attempts=automatic_attempts,
            operator_retries=operator_retries,
        )

        return availability_retry
