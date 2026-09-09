from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="CompiledLifecycle")



@_attrs_define
class CompiledLifecycle:
    """
        Attributes:
            post_stop (list[list[str]]):
            pre_start (list[list[str]]):
            stop_timeout_seconds (int):
     """

    post_stop: list[list[str]]
    pre_start: list[list[str]]
    stop_timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        post_stop = []
        for post_stop_item_data in self.post_stop:
            post_stop_item = post_stop_item_data


            post_stop.append(post_stop_item)



        pre_start = []
        for pre_start_item_data in self.pre_start:
            pre_start_item = pre_start_item_data


            pre_start.append(pre_start_item)



        stop_timeout_seconds = self.stop_timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "post_stop": post_stop,
            "pre_start": pre_start,
            "stop_timeout_seconds": stop_timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        post_stop = []
        _post_stop = d.pop("post_stop")
        for post_stop_item_data in (_post_stop):
            post_stop_item = cast(list[str], post_stop_item_data)

            post_stop.append(post_stop_item)


        pre_start = []
        _pre_start = d.pop("pre_start")
        for pre_start_item_data in (_pre_start):
            pre_start_item = cast(list[str], pre_start_item_data)

            pre_start.append(pre_start_item)


        stop_timeout_seconds = d.pop("stop_timeout_seconds")

        compiled_lifecycle = cls(
            post_stop=post_stop,
            pre_start=pre_start,
            stop_timeout_seconds=stop_timeout_seconds,
        )

        return compiled_lifecycle
