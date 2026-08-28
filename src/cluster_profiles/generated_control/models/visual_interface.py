from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.visual_interface_input import VisualInterfaceInput
  from ..models.visual_interface_output import VisualInterfaceOutput





T = TypeVar("T", bound="VisualInterface")



@_attrs_define
class VisualInterface:
    """
        Attributes:
            adapter (str):
            health_path (Union[None, Unset, str]):
            input_ (Union['VisualInterfaceInput', None, Unset]):
            model_aliases (Union[Unset, list[str]]):
            output (Union['VisualInterfaceOutput', None, Unset]):
            path (Union[None, Unset, str]):
            port (Union[None, Unset, int]):
            timeout_seconds (Union[None, Unset, int]):
     """

    adapter: str
    health_path: Union[None, Unset, str] = UNSET
    input_: Union['VisualInterfaceInput', None, Unset] = UNSET
    model_aliases: Union[Unset, list[str]] = UNSET
    output: Union['VisualInterfaceOutput', None, Unset] = UNSET
    path: Union[None, Unset, str] = UNSET
    port: Union[None, Unset, int] = UNSET
    timeout_seconds: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_interface_input import VisualInterfaceInput
        from ..models.visual_interface_output import VisualInterfaceOutput
        adapter = self.adapter

        health_path: Union[None, Unset, str]
        if isinstance(self.health_path, Unset):
            health_path = UNSET
        else:
            health_path = self.health_path

        input_: Union[None, Unset, dict[str, Any]]
        if isinstance(self.input_, Unset):
            input_ = UNSET
        elif isinstance(self.input_, VisualInterfaceInput):
            input_ = self.input_.to_dict()
        else:
            input_ = self.input_

        model_aliases: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model_aliases, Unset):
            model_aliases = self.model_aliases



        output: Union[None, Unset, dict[str, Any]]
        if isinstance(self.output, Unset):
            output = UNSET
        elif isinstance(self.output, VisualInterfaceOutput):
            output = self.output.to_dict()
        else:
            output = self.output

        path: Union[None, Unset, str]
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        port: Union[None, Unset, int]
        if isinstance(self.port, Unset):
            port = UNSET
        else:
            port = self.port

        timeout_seconds: Union[None, Unset, int]
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
        })
        if health_path is not UNSET:
            field_dict["health_path"] = health_path
        if input_ is not UNSET:
            field_dict["input"] = input_
        if model_aliases is not UNSET:
            field_dict["model_aliases"] = model_aliases
        if output is not UNSET:
            field_dict["output"] = output
        if path is not UNSET:
            field_dict["path"] = path
        if port is not UNSET:
            field_dict["port"] = port
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_interface_input import VisualInterfaceInput
        from ..models.visual_interface_output import VisualInterfaceOutput
        d = dict(src_dict)
        adapter = d.pop("adapter")

        def _parse_health_path(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        health_path = _parse_health_path(d.pop("health_path", UNSET))


        def _parse_input_(data: object) -> Union['VisualInterfaceInput', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                input_type_0 = VisualInterfaceInput.from_dict(data)



                return input_type_0
            except: # noqa: E722
                pass
            return cast(Union['VisualInterfaceInput', None, Unset], data)

        input_ = _parse_input_(d.pop("input", UNSET))


        model_aliases = cast(list[str], d.pop("model_aliases", UNSET))


        def _parse_output(data: object) -> Union['VisualInterfaceOutput', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                output_type_0 = VisualInterfaceOutput.from_dict(data)



                return output_type_0
            except: # noqa: E722
                pass
            return cast(Union['VisualInterfaceOutput', None, Unset], data)

        output = _parse_output(d.pop("output", UNSET))


        def _parse_path(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        path = _parse_path(d.pop("path", UNSET))


        def _parse_port(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        port = _parse_port(d.pop("port", UNSET))


        def _parse_timeout_seconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))


        visual_interface = cls(
            adapter=adapter,
            health_path=health_path,
            input_=input_,
            model_aliases=model_aliases,
            output=output,
            path=path,
            port=port,
            timeout_seconds=timeout_seconds,
        )

        return visual_interface
