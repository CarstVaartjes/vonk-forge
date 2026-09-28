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
            authority_revision (None | str | Unset):
            omitted_node_count (int | Unset):  Default: 0.
            payload_digest (None | str | Unset):
            plan_digest (None | str | Unset):
            rank (int | None | Unset):
     """

    attempt: int
    kind: str
    node_ids: list[str]
    operation_id: str
    source: EvidenceContextSource
    updated_at: str
    authority_revision: None | str | Unset = UNSET
    omitted_node_count: int | Unset = 0
    payload_digest: None | str | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    rank: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        attempt = self.attempt

        kind = self.kind

        node_ids = self.node_ids



        operation_id = self.operation_id

        source: str = self.source

        updated_at = self.updated_at

        authority_revision: None | str | Unset
        if isinstance(self.authority_revision, Unset):
            authority_revision = UNSET
        else:
            authority_revision = self.authority_revision

        omitted_node_count = self.omitted_node_count

        payload_digest: None | str | Unset
        if isinstance(self.payload_digest, Unset):
            payload_digest = UNSET
        else:
            payload_digest = self.payload_digest

        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

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
        if authority_revision is not UNSET:
            field_dict["authority_revision"] = authority_revision
        if omitted_node_count is not UNSET:
            field_dict["omitted_node_count"] = omitted_node_count
        if payload_digest is not UNSET:
            field_dict["payload_digest"] = payload_digest
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
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

        def _parse_authority_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        authority_revision = _parse_authority_revision(d.pop("authority_revision", UNSET))


        omitted_node_count = d.pop("omitted_node_count", UNSET)

        def _parse_payload_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        payload_digest = _parse_payload_digest(d.pop("payload_digest", UNSET))


        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


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
            authority_revision=authority_revision,
            omitted_node_count=omitted_node_count,
            payload_digest=payload_digest,
            plan_digest=plan_digest,
            rank=rank,
        )

        return evidence_context
