from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="JobLogsResponse")



@_attrs_define
class JobLogsResponse:
    """
        Attributes:
            digests (list[str]):
            job_id (str):
     """

    digests: list[str]
    job_id: str





    def to_dict(self) -> dict[str, Any]:
        digests = self.digests



        job_id = self.job_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "digests": digests,
            "job_id": job_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        digests = cast(list[str], d.pop("digests"))


        job_id = d.pop("job_id")

        job_logs_response = cls(
            digests=digests,
            job_id=job_id,
        )

        return job_logs_response
