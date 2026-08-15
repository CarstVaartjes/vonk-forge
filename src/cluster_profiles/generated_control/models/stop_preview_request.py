from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="StopPreviewRequest")



@_attrs_define
class StopPreviewRequest:
    """
        Attributes:
            run_id (str):
     """

    run_id: str





    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "run_id": run_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        run_id = d.pop("run_id")

        stop_preview_request = cls(
            run_id=run_id,
        )

        return stop_preview_request
