from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_environment_entry import CompiledEnvironmentEntry
  from ..models.compiled_runtime_telemetry import CompiledRuntimeTelemetry
  from ..models.compiled_placement import CompiledPlacement





T = TypeVar("T", bound="CompiledRuntime")



@_attrs_define
class CompiledRuntime:
    """
        Attributes:
            argv (list[str]):
            env (list['CompiledEnvironmentEntry']):
            executable (str):
            image_digest (str):
            placement (CompiledPlacement):
            telemetry (CompiledRuntimeTelemetry):
     """

    argv: list[str]
    env: list['CompiledEnvironmentEntry']
    executable: str
    image_digest: str
    placement: 'CompiledPlacement'
    telemetry: 'CompiledRuntimeTelemetry'





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_environment_entry import CompiledEnvironmentEntry
        from ..models.compiled_runtime_telemetry import CompiledRuntimeTelemetry
        from ..models.compiled_placement import CompiledPlacement
        argv = self.argv



        env = []
        for env_item_data in self.env:
            env_item = env_item_data.to_dict()
            env.append(env_item)



        executable = self.executable

        image_digest = self.image_digest

        placement = self.placement.to_dict()

        telemetry = self.telemetry.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "argv": argv,
            "env": env,
            "executable": executable,
            "image_digest": image_digest,
            "placement": placement,
            "telemetry": telemetry,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_environment_entry import CompiledEnvironmentEntry
        from ..models.compiled_runtime_telemetry import CompiledRuntimeTelemetry
        from ..models.compiled_placement import CompiledPlacement
        d = dict(src_dict)
        argv = cast(list[str], d.pop("argv"))


        env = []
        _env = d.pop("env")
        for env_item_data in (_env):
            env_item = CompiledEnvironmentEntry.from_dict(env_item_data)



            env.append(env_item)


        executable = d.pop("executable")

        image_digest = d.pop("image_digest")

        placement = CompiledPlacement.from_dict(d.pop("placement"))




        telemetry = CompiledRuntimeTelemetry.from_dict(d.pop("telemetry"))




        compiled_runtime = cls(
            argv=argv,
            env=env,
            executable=executable,
            image_digest=image_digest,
            placement=placement,
            telemetry=telemetry,
        )

        return compiled_runtime
