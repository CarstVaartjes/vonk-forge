from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.node_status_labels import NodeStatusLabels





T = TypeVar("T", bound="NodeStatus")



@_attrs_define
class NodeStatus:
    """
        Attributes:
            disk_available_bytes (int):
            display_name (str):
            healthy (Union[None, bool]):
            hostname (str):
            id (str):
            labels (NodeStatusLabels):
            lifecycle (str):
            memory_available_bytes (int):
            profile (Union[None, str]):
            stale (bool):
            agent_binary_digest (Union[None, Unset, str]):
            agent_build_digest (Union[None, Unset, str]):
            agent_last_seen_at (Union[None, Unset, str]):
            agent_online (Union[Unset, bool]):  Default: False.
            agent_semantic_version (Union[None, Unset, str]):
            agent_state (Union[Unset, str]):  Default: 'unregistered'.
            certificate_expires_at (Union[None, Unset, str]):
            certificate_expiry_seconds (Union[None, Unset, float]):
            compatibility (Union[Unset, str]):  Default: 'unknown'.
            inventory_age_seconds (Union[None, Unset, float]):
            inventory_capabilities (Union[Unset, list[str]]):
            inventory_observed_at (Union[None, Unset, str]):
            inventory_stale (Union[Unset, bool]):  Default: True.
            last_seen_age_seconds (Union[None, Unset, float]):
            last_seen_at (Union[None, Unset, str]):
            probe_age_seconds (Union[None, Unset, float]):
     """

    disk_available_bytes: int
    display_name: str
    healthy: Union[None, bool]
    hostname: str
    id: str
    labels: 'NodeStatusLabels'
    lifecycle: str
    memory_available_bytes: int
    profile: Union[None, str]
    stale: bool
    agent_binary_digest: Union[None, Unset, str] = UNSET
    agent_build_digest: Union[None, Unset, str] = UNSET
    agent_last_seen_at: Union[None, Unset, str] = UNSET
    agent_online: Union[Unset, bool] = False
    agent_semantic_version: Union[None, Unset, str] = UNSET
    agent_state: Union[Unset, str] = 'unregistered'
    certificate_expires_at: Union[None, Unset, str] = UNSET
    certificate_expiry_seconds: Union[None, Unset, float] = UNSET
    compatibility: Union[Unset, str] = 'unknown'
    inventory_age_seconds: Union[None, Unset, float] = UNSET
    inventory_capabilities: Union[Unset, list[str]] = UNSET
    inventory_observed_at: Union[None, Unset, str] = UNSET
    inventory_stale: Union[Unset, bool] = True
    last_seen_age_seconds: Union[None, Unset, float] = UNSET
    last_seen_at: Union[None, Unset, str] = UNSET
    probe_age_seconds: Union[None, Unset, float] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.node_status_labels import NodeStatusLabels
        disk_available_bytes = self.disk_available_bytes

        display_name = self.display_name

        healthy: Union[None, bool]
        healthy = self.healthy

        hostname = self.hostname

        id = self.id

        labels = self.labels.to_dict()

        lifecycle = self.lifecycle

        memory_available_bytes = self.memory_available_bytes

        profile: Union[None, str]
        profile = self.profile

        stale = self.stale

        agent_binary_digest: Union[None, Unset, str]
        if isinstance(self.agent_binary_digest, Unset):
            agent_binary_digest = UNSET
        else:
            agent_binary_digest = self.agent_binary_digest

        agent_build_digest: Union[None, Unset, str]
        if isinstance(self.agent_build_digest, Unset):
            agent_build_digest = UNSET
        else:
            agent_build_digest = self.agent_build_digest

        agent_last_seen_at: Union[None, Unset, str]
        if isinstance(self.agent_last_seen_at, Unset):
            agent_last_seen_at = UNSET
        else:
            agent_last_seen_at = self.agent_last_seen_at

        agent_online = self.agent_online

        agent_semantic_version: Union[None, Unset, str]
        if isinstance(self.agent_semantic_version, Unset):
            agent_semantic_version = UNSET
        else:
            agent_semantic_version = self.agent_semantic_version

        agent_state = self.agent_state

        certificate_expires_at: Union[None, Unset, str]
        if isinstance(self.certificate_expires_at, Unset):
            certificate_expires_at = UNSET
        else:
            certificate_expires_at = self.certificate_expires_at

        certificate_expiry_seconds: Union[None, Unset, float]
        if isinstance(self.certificate_expiry_seconds, Unset):
            certificate_expiry_seconds = UNSET
        else:
            certificate_expiry_seconds = self.certificate_expiry_seconds

        compatibility = self.compatibility

        inventory_age_seconds: Union[None, Unset, float]
        if isinstance(self.inventory_age_seconds, Unset):
            inventory_age_seconds = UNSET
        else:
            inventory_age_seconds = self.inventory_age_seconds

        inventory_capabilities: Union[Unset, list[str]] = UNSET
        if not isinstance(self.inventory_capabilities, Unset):
            inventory_capabilities = self.inventory_capabilities



        inventory_observed_at: Union[None, Unset, str]
        if isinstance(self.inventory_observed_at, Unset):
            inventory_observed_at = UNSET
        else:
            inventory_observed_at = self.inventory_observed_at

        inventory_stale = self.inventory_stale

        last_seen_age_seconds: Union[None, Unset, float]
        if isinstance(self.last_seen_age_seconds, Unset):
            last_seen_age_seconds = UNSET
        else:
            last_seen_age_seconds = self.last_seen_age_seconds

        last_seen_at: Union[None, Unset, str]
        if isinstance(self.last_seen_at, Unset):
            last_seen_at = UNSET
        else:
            last_seen_at = self.last_seen_at

        probe_age_seconds: Union[None, Unset, float]
        if isinstance(self.probe_age_seconds, Unset):
            probe_age_seconds = UNSET
        else:
            probe_age_seconds = self.probe_age_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "disk_available_bytes": disk_available_bytes,
            "display_name": display_name,
            "healthy": healthy,
            "hostname": hostname,
            "id": id,
            "labels": labels,
            "lifecycle": lifecycle,
            "memory_available_bytes": memory_available_bytes,
            "profile": profile,
            "stale": stale,
        })
        if agent_binary_digest is not UNSET:
            field_dict["agent_binary_digest"] = agent_binary_digest
        if agent_build_digest is not UNSET:
            field_dict["agent_build_digest"] = agent_build_digest
        if agent_last_seen_at is not UNSET:
            field_dict["agent_last_seen_at"] = agent_last_seen_at
        if agent_online is not UNSET:
            field_dict["agent_online"] = agent_online
        if agent_semantic_version is not UNSET:
            field_dict["agent_semantic_version"] = agent_semantic_version
        if agent_state is not UNSET:
            field_dict["agent_state"] = agent_state
        if certificate_expires_at is not UNSET:
            field_dict["certificate_expires_at"] = certificate_expires_at
        if certificate_expiry_seconds is not UNSET:
            field_dict["certificate_expiry_seconds"] = certificate_expiry_seconds
        if compatibility is not UNSET:
            field_dict["compatibility"] = compatibility
        if inventory_age_seconds is not UNSET:
            field_dict["inventory_age_seconds"] = inventory_age_seconds
        if inventory_capabilities is not UNSET:
            field_dict["inventory_capabilities"] = inventory_capabilities
        if inventory_observed_at is not UNSET:
            field_dict["inventory_observed_at"] = inventory_observed_at
        if inventory_stale is not UNSET:
            field_dict["inventory_stale"] = inventory_stale
        if last_seen_age_seconds is not UNSET:
            field_dict["last_seen_age_seconds"] = last_seen_age_seconds
        if last_seen_at is not UNSET:
            field_dict["last_seen_at"] = last_seen_at
        if probe_age_seconds is not UNSET:
            field_dict["probe_age_seconds"] = probe_age_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.node_status_labels import NodeStatusLabels
        d = dict(src_dict)
        disk_available_bytes = d.pop("disk_available_bytes")

        display_name = d.pop("display_name")

        def _parse_healthy(data: object) -> Union[None, bool]:
            if data is None:
                return data
            return cast(Union[None, bool], data)

        healthy = _parse_healthy(d.pop("healthy"))


        hostname = d.pop("hostname")

        id = d.pop("id")

        labels = NodeStatusLabels.from_dict(d.pop("labels"))




        lifecycle = d.pop("lifecycle")

        memory_available_bytes = d.pop("memory_available_bytes")

        def _parse_profile(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        profile = _parse_profile(d.pop("profile"))


        stale = d.pop("stale")

        def _parse_agent_binary_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        agent_binary_digest = _parse_agent_binary_digest(d.pop("agent_binary_digest", UNSET))


        def _parse_agent_build_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        agent_build_digest = _parse_agent_build_digest(d.pop("agent_build_digest", UNSET))


        def _parse_agent_last_seen_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        agent_last_seen_at = _parse_agent_last_seen_at(d.pop("agent_last_seen_at", UNSET))


        agent_online = d.pop("agent_online", UNSET)

        def _parse_agent_semantic_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        agent_semantic_version = _parse_agent_semantic_version(d.pop("agent_semantic_version", UNSET))


        agent_state = d.pop("agent_state", UNSET)

        def _parse_certificate_expires_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        certificate_expires_at = _parse_certificate_expires_at(d.pop("certificate_expires_at", UNSET))


        def _parse_certificate_expiry_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        certificate_expiry_seconds = _parse_certificate_expiry_seconds(d.pop("certificate_expiry_seconds", UNSET))


        compatibility = d.pop("compatibility", UNSET)

        def _parse_inventory_age_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        inventory_age_seconds = _parse_inventory_age_seconds(d.pop("inventory_age_seconds", UNSET))


        inventory_capabilities = cast(list[str], d.pop("inventory_capabilities", UNSET))


        def _parse_inventory_observed_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        inventory_observed_at = _parse_inventory_observed_at(d.pop("inventory_observed_at", UNSET))


        inventory_stale = d.pop("inventory_stale", UNSET)

        def _parse_last_seen_age_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        last_seen_age_seconds = _parse_last_seen_age_seconds(d.pop("last_seen_age_seconds", UNSET))


        def _parse_last_seen_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        last_seen_at = _parse_last_seen_at(d.pop("last_seen_at", UNSET))


        def _parse_probe_age_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        probe_age_seconds = _parse_probe_age_seconds(d.pop("probe_age_seconds", UNSET))


        node_status = cls(
            disk_available_bytes=disk_available_bytes,
            display_name=display_name,
            healthy=healthy,
            hostname=hostname,
            id=id,
            labels=labels,
            lifecycle=lifecycle,
            memory_available_bytes=memory_available_bytes,
            profile=profile,
            stale=stale,
            agent_binary_digest=agent_binary_digest,
            agent_build_digest=agent_build_digest,
            agent_last_seen_at=agent_last_seen_at,
            agent_online=agent_online,
            agent_semantic_version=agent_semantic_version,
            agent_state=agent_state,
            certificate_expires_at=certificate_expires_at,
            certificate_expiry_seconds=certificate_expiry_seconds,
            compatibility=compatibility,
            inventory_age_seconds=inventory_age_seconds,
            inventory_capabilities=inventory_capabilities,
            inventory_observed_at=inventory_observed_at,
            inventory_stale=inventory_stale,
            last_seen_age_seconds=last_seen_age_seconds,
            last_seen_at=last_seen_at,
            probe_age_seconds=probe_age_seconds,
        )

        return node_status
