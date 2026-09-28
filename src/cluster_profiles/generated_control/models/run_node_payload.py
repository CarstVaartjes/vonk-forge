from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunNodePayload")



@_attrs_define
class RunNodePayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['run-node']):
            node_id (str):
            rank (int):
            reserved_memory_bytes (int):
            role (str):
            run_id (str):
            state (str):
            observed_memory_bytes (int | None | Unset):
            schema_version (Literal[1] | Unset):  Default: 1.
     """

    entity_id: str
    entity_kind: Literal['run-node']
    node_id: str
    rank: int
    reserved_memory_bytes: int
    role: str
    run_id: str
    state: str
    observed_memory_bytes: int | None | Unset = UNSET
    schema_version: Literal[1] | Unset = 1





    def to_dict(self) -> dict[str, Any]:
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        node_id = self.node_id

        rank = self.rank

        reserved_memory_bytes = self.reserved_memory_bytes

        role = self.role

        run_id = self.run_id

        state = self.state

        observed_memory_bytes: int | None | Unset
        if isinstance(self.observed_memory_bytes, Unset):
            observed_memory_bytes = UNSET
        else:
            observed_memory_bytes = self.observed_memory_bytes

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "node_id": node_id,
            "rank": rank,
            "reserved_memory_bytes": reserved_memory_bytes,
            "role": role,
            "run_id": run_id,
            "state": state,
        })
        if observed_memory_bytes is not UNSET:
            field_dict["observed_memory_bytes"] = observed_memory_bytes
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['run-node'] , d.pop("entity_kind"))
        if entity_kind != 'run-node':
            raise ValueError(f"entity_kind must match const 'run-node', got '{entity_kind}'")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        reserved_memory_bytes = d.pop("reserved_memory_bytes")

        role = d.pop("role")

        run_id = d.pop("run_id")

        state = d.pop("state")

        def _parse_observed_memory_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        observed_memory_bytes = _parse_observed_memory_bytes(d.pop("observed_memory_bytes", UNSET))


        schema_version = cast(Literal[1] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        run_node_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            node_id=node_id,
            rank=rank,
            reserved_memory_bytes=reserved_memory_bytes,
            role=role,
            run_id=run_id,
            state=state,
            observed_memory_bytes=observed_memory_bytes,
            schema_version=schema_version,
        )

        return run_node_payload
