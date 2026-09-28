from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="AgentUpgradeIdentityResponse")



@_attrs_define
class AgentUpgradeIdentityResponse:
    """
        Attributes:
            binary_digest (None | str | Unset):
            build_digest (None | str | Unset):
            version (None | str | Unset):
     """

    binary_digest: None | str | Unset = UNSET
    build_digest: None | str | Unset = UNSET
    version: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        binary_digest: None | str | Unset
        if isinstance(self.binary_digest, Unset):
            binary_digest = UNSET
        else:
            binary_digest = self.binary_digest

        build_digest: None | str | Unset
        if isinstance(self.build_digest, Unset):
            build_digest = UNSET
        else:
            build_digest = self.build_digest

        version: None | str | Unset
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
        def _parse_binary_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        binary_digest = _parse_binary_digest(d.pop("binary_digest", UNSET))


        def _parse_build_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_digest = _parse_build_digest(d.pop("build_digest", UNSET))


        def _parse_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        version = _parse_version(d.pop("version", UNSET))


        agent_upgrade_identity_response = cls(
            binary_digest=binary_digest,
            build_digest=build_digest,
            version=version,
        )

        return agent_upgrade_identity_response
