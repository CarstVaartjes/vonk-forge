from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="AuditEventResponse")



@_attrs_define
class AuditEventResponse:
    """
        Attributes:
            action (str):
            actor (str):
            request_id (str):
            targets (list[str]):
            authority_revision (None | str | Unset):
            occurred_at (None | str | Unset):
     """

    action: str
    actor: str
    request_id: str
    targets: list[str]
    authority_revision: None | str | Unset = UNSET
    occurred_at: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        action = self.action

        actor = self.actor

        request_id = self.request_id

        targets = self.targets



        authority_revision: None | str | Unset
        if isinstance(self.authority_revision, Unset):
            authority_revision = UNSET
        else:
            authority_revision = self.authority_revision

        occurred_at: None | str | Unset
        if isinstance(self.occurred_at, Unset):
            occurred_at = UNSET
        else:
            occurred_at = self.occurred_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "actor": actor,
            "request_id": request_id,
            "targets": targets,
        })
        if authority_revision is not UNSET:
            field_dict["authority_revision"] = authority_revision
        if occurred_at is not UNSET:
            field_dict["occurred_at"] = occurred_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = d.pop("action")

        actor = d.pop("actor")

        request_id = d.pop("request_id")

        targets = cast(list[str], d.pop("targets"))


        def _parse_authority_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        authority_revision = _parse_authority_revision(d.pop("authority_revision", UNSET))


        def _parse_occurred_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        occurred_at = _parse_occurred_at(d.pop("occurred_at", UNSET))


        audit_event_response = cls(
            action=action,
            actor=actor,
            request_id=request_id,
            targets=targets,
            authority_revision=authority_revision,
            occurred_at=occurred_at,
        )

        return audit_event_response
