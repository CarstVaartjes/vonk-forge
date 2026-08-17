from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.visual_build_context import VisualBuildContext





T = TypeVar("T", bound="VisualBuild")



@_attrs_define
class VisualBuild:
    """
        Attributes:
            context (VisualBuildContext):
            dockerfile (str):
            download_bytes (int):
            memory_bytes (int):
            network_hosts (list[str]):
            network_mode (str):
            platform (str):
            temporary_bytes (int):
            timeout_seconds (int):
     """

    context: 'VisualBuildContext'
    dockerfile: str
    download_bytes: int
    memory_bytes: int
    network_hosts: list[str]
    network_mode: str
    platform: str
    temporary_bytes: int
    timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_build_context import VisualBuildContext
        context = self.context.to_dict()

        dockerfile = self.dockerfile

        download_bytes = self.download_bytes

        memory_bytes = self.memory_bytes

        network_hosts = self.network_hosts



        network_mode = self.network_mode

        platform = self.platform

        temporary_bytes = self.temporary_bytes

        timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "context": context,
            "dockerfile": dockerfile,
            "download_bytes": download_bytes,
            "memory_bytes": memory_bytes,
            "network_hosts": network_hosts,
            "network_mode": network_mode,
            "platform": platform,
            "temporary_bytes": temporary_bytes,
            "timeout_seconds": timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_build_context import VisualBuildContext
        d = dict(src_dict)
        context = VisualBuildContext.from_dict(d.pop("context"))




        dockerfile = d.pop("dockerfile")

        download_bytes = d.pop("download_bytes")

        memory_bytes = d.pop("memory_bytes")

        network_hosts = cast(list[str], d.pop("network_hosts"))


        network_mode = d.pop("network_mode")

        platform = d.pop("platform")

        temporary_bytes = d.pop("temporary_bytes")

        timeout_seconds = d.pop("timeout_seconds")

        visual_build = cls(
            context=context,
            dockerfile=dockerfile,
            download_bytes=download_bytes,
            memory_bytes=memory_bytes,
            network_hosts=network_hosts,
            network_mode=network_mode,
            platform=platform,
            temporary_bytes=temporary_bytes,
            timeout_seconds=timeout_seconds,
        )

        return visual_build
