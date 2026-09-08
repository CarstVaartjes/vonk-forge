from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, cast
from typing import Literal, Union, cast






T = TypeVar("T", bound="InstallationNodePayload")



@_attrs_define
class InstallationNodePayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['installation-node']):
            installation_id (str):
            installed_bytes (int):
            node_id (str):
            rank (int):
            required_bytes (int):
            role (str):
            state (str):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    entity_id: str
    entity_kind: Literal['installation-node']
    installation_id: str
    installed_bytes: int
    node_id: str
    rank: int
    required_bytes: int
    role: str
    state: str
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        installation_id = self.installation_id

        installed_bytes = self.installed_bytes

        node_id = self.node_id

        rank = self.rank

        required_bytes = self.required_bytes

        role = self.role

        state = self.state

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "installation_id": installation_id,
            "installed_bytes": installed_bytes,
            "node_id": node_id,
            "rank": rank,
            "required_bytes": required_bytes,
            "role": role,
            "state": state,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['installation-node'] , d.pop("entity_kind"))
        if entity_kind != 'installation-node':
            raise ValueError(f"entity_kind must match const 'installation-node', got '{entity_kind}'")

        installation_id = d.pop("installation_id")

        installed_bytes = d.pop("installed_bytes")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        required_bytes = d.pop("required_bytes")

        role = d.pop("role")

        state = d.pop("state")

        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        installation_node_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            installation_id=installation_id,
            installed_bytes=installed_bytes,
            node_id=node_id,
            rank=rank,
            required_bytes=required_bytes,
            role=role,
            state=state,
            schema_version=schema_version,
        )

        return installation_node_payload
