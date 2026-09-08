from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="CompiledEndpoint")



@_attrs_define
class CompiledEndpoint:
    """
        Attributes:
            health_path (str):
            model_aliases (list[str]):
            port (int):
            protocol (Literal['openai']):
     """

    health_path: str
    model_aliases: list[str]
    port: int
    protocol: Literal['openai']





    def to_dict(self) -> dict[str, Any]:
        health_path = self.health_path

        model_aliases = self.model_aliases



        port = self.port

        protocol = self.protocol


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "health_path": health_path,
            "model_aliases": model_aliases,
            "port": port,
            "protocol": protocol,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        health_path = d.pop("health_path")

        model_aliases = cast(list[str], d.pop("model_aliases"))


        port = d.pop("port")

        protocol = cast(Literal['openai'] , d.pop("protocol"))
        if protocol != 'openai':
            raise ValueError(f"protocol must match const 'openai', got '{protocol}'")

        compiled_endpoint = cls(
            health_path=health_path,
            model_aliases=model_aliases,
            port=port,
            protocol=protocol,
        )

        return compiled_endpoint
