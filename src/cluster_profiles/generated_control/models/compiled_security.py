from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_security_network_mode import check_compiled_security_network_mode
from ..models.compiled_security_network_mode import CompiledSecurityNetworkMode
from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_security_mount import CompiledSecurityMount





T = TypeVar("T", bound="CompiledSecurity")



@_attrs_define
class CompiledSecurity:
    """ Per-workload security choices; everything else is a platform constant.

        Attributes:
            gpu (bool):
            mounts (list[CompiledSecurityMount]):
            network_mode (CompiledSecurityNetworkMode):
            user (str):
     """

    gpu: bool
    mounts: list[CompiledSecurityMount]
    network_mode: CompiledSecurityNetworkMode
    user: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_security_mount import CompiledSecurityMount # noqa: PLC0415
        gpu = self.gpu

        mounts = []
        for mounts_item_data in self.mounts:
            mounts_item = mounts_item_data.to_dict()
            mounts.append(mounts_item)



        network_mode: str = self.network_mode

        user = self.user


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "gpu": gpu,
            "mounts": mounts,
            "network_mode": network_mode,
            "user": user,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_security_mount import CompiledSecurityMount # noqa: PLC0415
        d = dict(src_dict)
        gpu = d.pop("gpu")

        mounts = []
        _mounts = d.pop("mounts")
        for mounts_item_data in (_mounts):
            mounts_item = CompiledSecurityMount.from_dict(mounts_item_data)



            mounts.append(mounts_item)


        network_mode = check_compiled_security_network_mode(d.pop("network_mode"))




        user = d.pop("user")

        compiled_security = cls(
            gpu=gpu,
            mounts=mounts,
            network_mode=network_mode,
            user=user,
        )

        return compiled_security
