from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="AgentSummary")



@_attrs_define
class AgentSummary:
    """
        Attributes:
            capabilities (list[str]):
            certificate_expires_at (Union[None, str]):
            last_seen_at (Union[None, str]):
            node_id (str):
            stale (bool):
            state (str):
            binary_digest (Union[None, Unset, str]):
            build_digest (Union[None, Unset, str]):
            last_seen_age_seconds (Union[None, Unset, float]):
            protocol_version (Union[None, Unset, int]):
            semantic_version (Union[None, Unset, str]):
     """

    capabilities: list[str]
    certificate_expires_at: Union[None, str]
    last_seen_at: Union[None, str]
    node_id: str
    stale: bool
    state: str
    binary_digest: Union[None, Unset, str] = UNSET
    build_digest: Union[None, Unset, str] = UNSET
    last_seen_age_seconds: Union[None, Unset, float] = UNSET
    protocol_version: Union[None, Unset, int] = UNSET
    semantic_version: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        capabilities = self.capabilities



        certificate_expires_at: Union[None, str]
        certificate_expires_at = self.certificate_expires_at

        last_seen_at: Union[None, str]
        last_seen_at = self.last_seen_at

        node_id = self.node_id

        stale = self.stale

        state = self.state

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

        last_seen_age_seconds: Union[None, Unset, float]
        if isinstance(self.last_seen_age_seconds, Unset):
            last_seen_age_seconds = UNSET
        else:
            last_seen_age_seconds = self.last_seen_age_seconds

        protocol_version: Union[None, Unset, int]
        if isinstance(self.protocol_version, Unset):
            protocol_version = UNSET
        else:
            protocol_version = self.protocol_version

        semantic_version: Union[None, Unset, str]
        if isinstance(self.semantic_version, Unset):
            semantic_version = UNSET
        else:
            semantic_version = self.semantic_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "certificate_expires_at": certificate_expires_at,
            "last_seen_at": last_seen_at,
            "node_id": node_id,
            "stale": stale,
            "state": state,
        })
        if binary_digest is not UNSET:
            field_dict["binary_digest"] = binary_digest
        if build_digest is not UNSET:
            field_dict["build_digest"] = build_digest
        if last_seen_age_seconds is not UNSET:
            field_dict["last_seen_age_seconds"] = last_seen_age_seconds
        if protocol_version is not UNSET:
            field_dict["protocol_version"] = protocol_version
        if semantic_version is not UNSET:
            field_dict["semantic_version"] = semantic_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        def _parse_certificate_expires_at(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        certificate_expires_at = _parse_certificate_expires_at(d.pop("certificate_expires_at"))


        def _parse_last_seen_at(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        last_seen_at = _parse_last_seen_at(d.pop("last_seen_at"))


        node_id = d.pop("node_id")

        stale = d.pop("stale")

        state = d.pop("state")

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


        def _parse_last_seen_age_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        last_seen_age_seconds = _parse_last_seen_age_seconds(d.pop("last_seen_age_seconds", UNSET))


        def _parse_protocol_version(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        protocol_version = _parse_protocol_version(d.pop("protocol_version", UNSET))


        def _parse_semantic_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        semantic_version = _parse_semantic_version(d.pop("semantic_version", UNSET))


        agent_summary = cls(
            capabilities=capabilities,
            certificate_expires_at=certificate_expires_at,
            last_seen_at=last_seen_at,
            node_id=node_id,
            stale=stale,
            state=state,
            binary_digest=binary_digest,
            build_digest=build_digest,
            last_seen_age_seconds=last_seen_age_seconds,
            protocol_version=protocol_version,
            semantic_version=semantic_version,
        )

        return agent_summary
