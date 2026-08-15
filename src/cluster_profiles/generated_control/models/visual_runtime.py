from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VisualRuntime")



@_attrs_define
class VisualRuntime:
    """
        Attributes:
            adapter (str):
            adapter_version (int):
            endpoint_port (int):
            endpoint_protocol (str):
            health_path (str):
            interface (str):
            model_aliases (list[str]):
     """

    adapter: str
    adapter_version: int
    endpoint_port: int
    endpoint_protocol: str
    health_path: str
    interface: str
    model_aliases: list[str]





    def to_dict(self) -> dict[str, Any]:
        adapter = self.adapter

        adapter_version = self.adapter_version

        endpoint_port = self.endpoint_port

        endpoint_protocol = self.endpoint_protocol

        health_path = self.health_path

        interface = self.interface

        model_aliases = self.model_aliases




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
            "adapter_version": adapter_version,
            "endpoint_port": endpoint_port,
            "endpoint_protocol": endpoint_protocol,
            "health_path": health_path,
            "interface": interface,
            "model_aliases": model_aliases,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter = d.pop("adapter")

        adapter_version = d.pop("adapter_version")

        endpoint_port = d.pop("endpoint_port")

        endpoint_protocol = d.pop("endpoint_protocol")

        health_path = d.pop("health_path")

        interface = d.pop("interface")

        model_aliases = cast(list[str], d.pop("model_aliases"))


        visual_runtime = cls(
            adapter=adapter,
            adapter_version=adapter_version,
            endpoint_port=endpoint_port,
            endpoint_protocol=endpoint_protocol,
            health_path=health_path,
            interface=interface,
            model_aliases=model_aliases,
        )

        return visual_runtime
