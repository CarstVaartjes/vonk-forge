from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RecipeOpenAIInterface")



@_attrs_define
class RecipeOpenAIInterface:
    """
        Attributes:
            adapter (Literal['openai']):
            health_path (str):
            model_aliases (list[str]):
            port (int):
     """

    adapter: Literal['openai']
    health_path: str
    model_aliases: list[str]
    port: int





    def to_dict(self) -> dict[str, Any]:
        adapter = self.adapter

        health_path = self.health_path

        model_aliases = self.model_aliases



        port = self.port


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
            "health_path": health_path,
            "model_aliases": model_aliases,
            "port": port,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter = cast(Literal['openai'] , d.pop("adapter"))
        if adapter != 'openai':
            raise ValueError(f"adapter must match const 'openai', got '{adapter}'")

        health_path = d.pop("health_path")

        model_aliases = cast(list[str], d.pop("model_aliases"))


        port = d.pop("port")

        recipe_open_ai_interface = cls(
            adapter=adapter,
            health_path=health_path,
            model_aliases=model_aliases,
            port=port,
        )

        return recipe_open_ai_interface
