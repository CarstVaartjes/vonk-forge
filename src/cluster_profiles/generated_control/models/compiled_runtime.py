from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_environment_entry import CompiledEnvironmentEntry
  from ..models.compiled_placement import CompiledPlacement





T = TypeVar("T", bound="CompiledRuntime")



@_attrs_define
class CompiledRuntime:
    """
        Attributes:
            argv (list[str]):
            env (list[CompiledEnvironmentEntry]):
            executable (str):
            placement (CompiledPlacement):
     """

    argv: list[str]
    env: list[CompiledEnvironmentEntry]
    executable: str
    placement: CompiledPlacement





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_environment_entry import CompiledEnvironmentEntry # noqa: PLC0415
        from ..models.compiled_placement import CompiledPlacement # noqa: PLC0415
        argv = self.argv



        env = []
        for env_item_data in self.env:
            env_item = env_item_data.to_dict()
            env.append(env_item)



        executable = self.executable

        placement = self.placement.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "argv": argv,
            "env": env,
            "executable": executable,
            "placement": placement,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_environment_entry import CompiledEnvironmentEntry # noqa: PLC0415
        from ..models.compiled_placement import CompiledPlacement # noqa: PLC0415
        d = dict(src_dict)
        argv = cast(list[str], d.pop("argv"))


        env = []
        _env = d.pop("env")
        for env_item_data in (_env):
            env_item = CompiledEnvironmentEntry.from_dict(env_item_data)



            env.append(env_item)


        executable = d.pop("executable")

        placement = CompiledPlacement.from_dict(d.pop("placement"))




        compiled_runtime = cls(
            argv=argv,
            env=env,
            executable=executable,
            placement=placement,
        )

        return compiled_runtime
