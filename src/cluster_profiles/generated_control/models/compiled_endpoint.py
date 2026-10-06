from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="CompiledEndpoint")



@_attrs_define
class CompiledEndpoint:
    """ An OpenAI-compatible endpoint.

        Attributes:
            health_path (str):
            model_aliases (list[str]):
            port (int):
     """

    health_path: str
    model_aliases: list[str]
    port: int





    def to_dict(self) -> dict[str, Any]:
        health_path = self.health_path

        model_aliases = self.model_aliases



        port = self.port


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "health_path": health_path,
            "model_aliases": model_aliases,
            "port": port,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        health_path = d.pop("health_path")

        model_aliases = cast(list[str], d.pop("model_aliases"))


        port = d.pop("port")

        compiled_endpoint = cls(
            health_path=health_path,
            model_aliases=model_aliases,
            port=port,
        )

        return compiled_endpoint
