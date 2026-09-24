from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ConditionalPostStopMemoryCheck")



@_attrs_define
class ConditionalPostStopMemoryCheck:
    """ Fresh inventory and ordinary memory admission required after stops.

        Attributes:
            stop_run_ids (list[str]):
     """

    stop_run_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        stop_run_ids = self.stop_run_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "stop_run_ids": stop_run_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stop_run_ids = cast(list[str], d.pop("stop_run_ids"))


        conditional_post_stop_memory_check = cls(
            stop_run_ids=stop_run_ids,
        )

        return conditional_post_stop_memory_check
