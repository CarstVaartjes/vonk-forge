from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="VisualInterface")



@_attrs_define
class VisualInterface:
    """
        Attributes:
            adapter (str):
            health_path (Union[None, Unset, str]):
            model_aliases (Union[Unset, list[str]]):
            path (Union[None, Unset, str]):
            port (Union[None, Unset, int]):
     """

    adapter: str
    health_path: Union[None, Unset, str] = UNSET
    model_aliases: Union[Unset, list[str]] = UNSET
    path: Union[None, Unset, str] = UNSET
    port: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        adapter = self.adapter

        health_path: Union[None, Unset, str]
        if isinstance(self.health_path, Unset):
            health_path = UNSET
        else:
            health_path = self.health_path

        model_aliases: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model_aliases, Unset):
            model_aliases = self.model_aliases



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


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
        })
        if health_path is not UNSET:
            field_dict["health_path"] = health_path
        if model_aliases is not UNSET:
            field_dict["model_aliases"] = model_aliases
        if path is not UNSET:
            field_dict["path"] = path
        if port is not UNSET:
            field_dict["port"] = port

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter = d.pop("adapter")

        def _parse_health_path(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        health_path = _parse_health_path(d.pop("health_path", UNSET))


        model_aliases = cast(list[str], d.pop("model_aliases", UNSET))


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


        visual_interface = cls(
            adapter=adapter,
            health_path=health_path,
            model_aliases=model_aliases,
            path=path,
            port=port,
        )

        return visual_interface
