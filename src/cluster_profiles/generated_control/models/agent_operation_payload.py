from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="AgentOperationPayload")



@_attrs_define
class AgentOperationPayload:
    """
        Attributes:
            attempt (int):
            entity_id (str):
            entity_kind (Literal['agent-operation']):
            kind (str):
            node_id (str):
            parent_job_id (str):
            state (str):
     """

    attempt: int
    entity_id: str
    entity_kind: Literal['agent-operation']
    kind: str
    node_id: str
    parent_job_id: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        attempt = self.attempt

        entity_id = self.entity_id

        entity_kind = self.entity_kind

        kind = self.kind

        node_id = self.node_id

        parent_job_id = self.parent_job_id

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "kind": kind,
            "node_id": node_id,
            "parent_job_id": parent_job_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attempt = d.pop("attempt")

        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['agent-operation'] , d.pop("entity_kind"))
        if entity_kind != 'agent-operation':
            raise ValueError(f"entity_kind must match const 'agent-operation', got '{entity_kind}'")

        kind = d.pop("kind")

        node_id = d.pop("node_id")

        parent_job_id = d.pop("parent_job_id")

        state = d.pop("state")

        agent_operation_payload = cls(
            attempt=attempt,
            entity_id=entity_id,
            entity_kind=entity_kind,
            kind=kind,
            node_id=node_id,
            parent_job_id=parent_job_id,
            state=state,
        )

        return agent_operation_payload
