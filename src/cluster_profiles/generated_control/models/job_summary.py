from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
import datetime






T = TypeVar("T", bound="JobSummary")



@_attrs_define
class JobSummary:
    """
        Attributes:
            created_at (datetime.datetime):
            id (str):
            kind (str):
            state (str):
     """

    created_at: datetime.datetime
    id: str
    kind: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        created_at = self.created_at.isoformat()

        id = self.id

        kind = self.kind

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "created_at": created_at,
            "id": id,
            "kind": kind,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created_at = isoparse(d.pop("created_at"))




        id = d.pop("id")

        kind = d.pop("kind")

        state = d.pop("state")

        job_summary = cls(
            created_at=created_at,
            id=id,
            kind=kind,
            state=state,
        )

        return job_summary
