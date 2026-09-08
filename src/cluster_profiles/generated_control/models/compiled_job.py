from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_job_interface import check_compiled_job_interface
from ..models.compiled_job_interface import CompiledJobInterface
from typing import cast
from typing import cast, Union
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.compiled_job_input import CompiledJobInput





T = TypeVar("T", bound="CompiledJob")



@_attrs_define
class CompiledJob:
    """
        Attributes:
            input_ (Union['CompiledJobInput', None]):
            interface (CompiledJobInterface):
            output_path (Literal['/outputs']):
            timeout_seconds (int):
     """

    input_: Union['CompiledJobInput', None]
    interface: CompiledJobInterface
    output_path: Literal['/outputs']
    timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_job_input import CompiledJobInput
        input_: Union[None, dict[str, Any]]
        if isinstance(self.input_, CompiledJobInput):
            input_ = self.input_.to_dict()
        else:
            input_ = self.input_

        interface: str = self.interface

        output_path = self.output_path

        timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "input": input_,
            "interface": interface,
            "output_path": output_path,
            "timeout_seconds": timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_job_input import CompiledJobInput
        d = dict(src_dict)
        def _parse_input_(data: object) -> Union['CompiledJobInput', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                input_type_0 = CompiledJobInput.from_dict(data)



                return input_type_0
            except: # noqa: E722
                pass
            return cast(Union['CompiledJobInput', None], data)

        input_ = _parse_input_(d.pop("input"))


        interface = check_compiled_job_interface(d.pop("interface"))




        output_path = cast(Literal['/outputs'] , d.pop("output_path"))
        if output_path != '/outputs':
            raise ValueError(f"output_path must match const '/outputs', got '{output_path}'")

        timeout_seconds = d.pop("timeout_seconds")

        compiled_job = cls(
            input_=input_,
            interface=interface,
            output_path=output_path,
            timeout_seconds=timeout_seconds,
        )

        return compiled_job
