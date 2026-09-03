from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.visual_build_context import VisualBuildContext
  from ..models.visual_build_options import VisualBuildOptions





T = TypeVar("T", bound="VisualBuild")



@_attrs_define
class VisualBuild:
    """
        Attributes:
            capabilities (list[str]):
            context (VisualBuildContext):
            cpu_cores (int):
            dockerfile (str):
            download_bytes (int):
            memory_bytes (int):
            network_hosts (list[str]):
            network_mode (str):
            options (VisualBuildOptions):
            platform (str):
            processes (int):
            target (Union[None, str]):
            temporary_bytes (int):
            timeout_seconds (int):
     """

    capabilities: list[str]
    context: 'VisualBuildContext'
    cpu_cores: int
    dockerfile: str
    download_bytes: int
    memory_bytes: int
    network_hosts: list[str]
    network_mode: str
    options: 'VisualBuildOptions'
    platform: str
    processes: int
    target: Union[None, str]
    temporary_bytes: int
    timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_build_context import VisualBuildContext
        from ..models.visual_build_options import VisualBuildOptions
        capabilities = self.capabilities



        context = self.context.to_dict()

        cpu_cores = self.cpu_cores

        dockerfile = self.dockerfile

        download_bytes = self.download_bytes

        memory_bytes = self.memory_bytes

        network_hosts = self.network_hosts



        network_mode = self.network_mode

        options = self.options.to_dict()

        platform = self.platform

        processes = self.processes

        target: Union[None, str]
        target = self.target

        temporary_bytes = self.temporary_bytes

        timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "context": context,
            "cpu_cores": cpu_cores,
            "dockerfile": dockerfile,
            "download_bytes": download_bytes,
            "memory_bytes": memory_bytes,
            "network_hosts": network_hosts,
            "network_mode": network_mode,
            "options": options,
            "platform": platform,
            "processes": processes,
            "target": target,
            "temporary_bytes": temporary_bytes,
            "timeout_seconds": timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_build_context import VisualBuildContext
        from ..models.visual_build_options import VisualBuildOptions
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        context = VisualBuildContext.from_dict(d.pop("context"))




        cpu_cores = d.pop("cpu_cores")

        dockerfile = d.pop("dockerfile")

        download_bytes = d.pop("download_bytes")

        memory_bytes = d.pop("memory_bytes")

        network_hosts = cast(list[str], d.pop("network_hosts"))


        network_mode = d.pop("network_mode")

        options = VisualBuildOptions.from_dict(d.pop("options"))




        platform = d.pop("platform")

        processes = d.pop("processes")

        def _parse_target(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        target = _parse_target(d.pop("target"))


        temporary_bytes = d.pop("temporary_bytes")

        timeout_seconds = d.pop("timeout_seconds")

        visual_build = cls(
            capabilities=capabilities,
            context=context,
            cpu_cores=cpu_cores,
            dockerfile=dockerfile,
            download_bytes=download_bytes,
            memory_bytes=memory_bytes,
            network_hosts=network_hosts,
            network_mode=network_mode,
            options=options,
            platform=platform,
            processes=processes,
            target=target,
            temporary_bytes=temporary_bytes,
            timeout_seconds=timeout_seconds,
        )

        return visual_build
