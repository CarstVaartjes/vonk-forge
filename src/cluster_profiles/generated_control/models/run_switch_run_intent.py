from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.run_switch_apply_request import RunSwitchApplyRequest





T = TypeVar("T", bound="RunSwitchRunIntent")



@_attrs_define
class RunSwitchRunIntent:
    """
        Attributes:
            request (RunSwitchApplyRequest):
            type_ (Literal['run']):
     """

    request: RunSwitchApplyRequest
    type_: Literal['run']





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_apply_request import RunSwitchApplyRequest # noqa: PLC0415
        request = self.request.to_dict()

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request": request,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_apply_request import RunSwitchApplyRequest # noqa: PLC0415
        d = dict(src_dict)
        request = RunSwitchApplyRequest.from_dict(d.pop("request"))




        type_ = cast(Literal['run'] , d.pop("type"))
        if type_ != 'run':
            raise ValueError(f"type must match const 'run', got '{type_}'")

        run_switch_run_intent = cls(
            request=request,
            type_=type_,
        )

        return run_switch_run_intent
