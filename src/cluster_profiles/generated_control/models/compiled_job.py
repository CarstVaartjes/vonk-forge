from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_job_interface import check_compiled_job_interface
from ..models.compiled_job_interface import CompiledJobInterface
from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_job_input import CompiledJobInput





T = TypeVar("T", bound="CompiledJob")



@_attrs_define
class CompiledJob:
    """
        Attributes:
            input_ (CompiledJobInput | None):
            interface (CompiledJobInterface):
            timeout_seconds (int):
     """

    input_: CompiledJobInput | None
    interface: CompiledJobInterface
    timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_job_input import CompiledJobInput # noqa: PLC0415
        input_: dict[str, Any] | None
        if isinstance(self.input_, CompiledJobInput):
            input_ = self.input_.to_dict()
        else:
            input_ = self.input_

        interface: str = self.interface

        timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "input": input_,
            "interface": interface,
            "timeout_seconds": timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_job_input import CompiledJobInput # noqa: PLC0415
        d = dict(src_dict)
        def _parse_input_(data: object) -> CompiledJobInput | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                input_type_0 = CompiledJobInput.from_dict(data)



                return input_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledJobInput | None, data)

        input_ = _parse_input_(d.pop("input"))


        interface = check_compiled_job_interface(d.pop("interface"))




        timeout_seconds = d.pop("timeout_seconds")

        compiled_job = cls(
            input_=input_,
            interface=interface,
            timeout_seconds=timeout_seconds,
        )

        return compiled_job
