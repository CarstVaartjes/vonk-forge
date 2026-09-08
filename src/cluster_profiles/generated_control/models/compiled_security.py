from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

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
    """
        Attributes:
            capabilities (list[str]):
            devices (list[str]):
            host_network (bool):
            mounts (list['CompiledSecurityMount']):
            network_mode (CompiledSecurityNetworkMode):
            no_new_privileges (bool):
            privileged (bool):
            read_only_root (bool):
            user (str):
     """

    capabilities: list[str]
    devices: list[str]
    host_network: bool
    mounts: list['CompiledSecurityMount']
    network_mode: CompiledSecurityNetworkMode
    no_new_privileges: bool
    privileged: bool
    read_only_root: bool
    user: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_security_mount import CompiledSecurityMount
        capabilities = self.capabilities



        devices = self.devices



        host_network = self.host_network

        mounts = []
        for mounts_item_data in self.mounts:
            mounts_item = mounts_item_data.to_dict()
            mounts.append(mounts_item)



        network_mode: str = self.network_mode

        no_new_privileges = self.no_new_privileges

        privileged = self.privileged

        read_only_root = self.read_only_root

        user = self.user


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "devices": devices,
            "host_network": host_network,
            "mounts": mounts,
            "network_mode": network_mode,
            "no_new_privileges": no_new_privileges,
            "privileged": privileged,
            "read_only_root": read_only_root,
            "user": user,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_security_mount import CompiledSecurityMount
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        devices = cast(list[str], d.pop("devices"))


        host_network = d.pop("host_network")

        mounts = []
        _mounts = d.pop("mounts")
        for mounts_item_data in (_mounts):
            mounts_item = CompiledSecurityMount.from_dict(mounts_item_data)



            mounts.append(mounts_item)


        network_mode = check_compiled_security_network_mode(d.pop("network_mode"))




        no_new_privileges = d.pop("no_new_privileges")

        privileged = d.pop("privileged")

        read_only_root = d.pop("read_only_root")

        user = d.pop("user")

        compiled_security = cls(
            capabilities=capabilities,
            devices=devices,
            host_network=host_network,
            mounts=mounts,
            network_mode=network_mode,
            no_new_privileges=no_new_privileges,
            privileged=privileged,
            read_only_root=read_only_root,
            user=user,
        )

        return compiled_security
