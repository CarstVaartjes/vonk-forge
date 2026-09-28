from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="JobPayload")



@_attrs_define
class JobPayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['job']):
            kind (str):
            state (str):
            target_count (int):
     """

    entity_id: str
    entity_kind: Literal['job']
    kind: str
    state: str
    target_count: int





    def to_dict(self) -> dict[str, Any]:
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        kind = self.kind

        state = self.state

        target_count = self.target_count


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "kind": kind,
            "state": state,
            "target_count": target_count,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['job'] , d.pop("entity_kind"))
        if entity_kind != 'job':
            raise ValueError(f"entity_kind must match const 'job', got '{entity_kind}'")

        kind = d.pop("kind")

        state = d.pop("state")

        target_count = d.pop("target_count")

        job_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            kind=kind,
            state=state,
            target_count=target_count,
        )

        return job_payload
