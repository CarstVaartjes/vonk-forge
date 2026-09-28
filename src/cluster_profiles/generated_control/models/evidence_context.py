from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.evidence_context_source import check_evidence_context_source
from ..models.evidence_context_source import EvidenceContextSource
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="EvidenceContext")



@_attrs_define
class EvidenceContext:
    """
        Attributes:
            attempt (int):
            kind (str):
            node_ids (list[str]):
            operation_id (str):
            source (EvidenceContextSource):
            updated_at (str):
            rank (int | None | Unset):
     """

    attempt: int
    kind: str
    node_ids: list[str]
    operation_id: str
    source: EvidenceContextSource
    updated_at: str
    rank: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        attempt = self.attempt

        kind = self.kind

        node_ids = self.node_ids



        operation_id = self.operation_id

        source: str = self.source

        updated_at = self.updated_at

        rank: int | None | Unset
        if isinstance(self.rank, Unset):
            rank = UNSET
        else:
            rank = self.rank


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "kind": kind,
            "node_ids": node_ids,
            "operation_id": operation_id,
            "source": source,
            "updated_at": updated_at,
        })
        if rank is not UNSET:
            field_dict["rank"] = rank

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attempt = d.pop("attempt")

        kind = d.pop("kind")

        node_ids = cast(list[str], d.pop("node_ids"))


        operation_id = d.pop("operation_id")

        source = check_evidence_context_source(d.pop("source"))




        updated_at = d.pop("updated_at")

        def _parse_rank(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        rank = _parse_rank(d.pop("rank", UNSET))


        evidence_context = cls(
            attempt=attempt,
            kind=kind,
            node_ids=node_ids,
            operation_id=operation_id,
            source=source,
            updated_at=updated_at,
            rank=rank,
        )

        return evidence_context
