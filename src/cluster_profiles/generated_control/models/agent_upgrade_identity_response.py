from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="AgentUpgradeIdentityResponse")



@_attrs_define
class AgentUpgradeIdentityResponse:
    """
        Attributes:
            binary_digest (Union[None, Unset, str]):
            build_digest (Union[None, Unset, str]):
            version (Union[None, Unset, str]):
     """

    binary_digest: Union[None, Unset, str] = UNSET
    build_digest: Union[None, Unset, str] = UNSET
    version: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        binary_digest: Union[None, Unset, str]
        if isinstance(self.binary_digest, Unset):
            binary_digest = UNSET
        else:
            binary_digest = self.binary_digest

        build_digest: Union[None, Unset, str]
        if isinstance(self.build_digest, Unset):
            build_digest = UNSET
        else:
            build_digest = self.build_digest

        version: Union[None, Unset, str]
        if isinstance(self.version, Unset):
            version = UNSET
        else:
            version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if binary_digest is not UNSET:
            field_dict["binary_digest"] = binary_digest
        if build_digest is not UNSET:
            field_dict["build_digest"] = build_digest
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_binary_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        binary_digest = _parse_binary_digest(d.pop("binary_digest", UNSET))


        def _parse_build_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_digest = _parse_build_digest(d.pop("build_digest", UNSET))


        def _parse_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        version = _parse_version(d.pop("version", UNSET))


        agent_upgrade_identity_response = cls(
            binary_digest=binary_digest,
            build_digest=build_digest,
            version=version,
        )

        return agent_upgrade_identity_response
