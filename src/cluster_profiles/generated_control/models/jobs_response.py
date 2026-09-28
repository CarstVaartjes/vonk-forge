from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.job_summary import JobSummary





T = TypeVar("T", bound="JobsResponse")



@_attrs_define
class JobsResponse:
    """
        Attributes:
            jobs (list[JobSummary]):
            total (int):
            next_cursor (None | str | Unset):
     """

    jobs: list[JobSummary]
    total: int
    next_cursor: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_summary import JobSummary # noqa: PLC0415
        jobs = []
        for jobs_item_data in self.jobs:
            jobs_item = jobs_item_data.to_dict()
            jobs.append(jobs_item)



        total = self.total

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "jobs": jobs,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_summary import JobSummary # noqa: PLC0415
        d = dict(src_dict)
        jobs = []
        _jobs = d.pop("jobs")
        for jobs_item_data in (_jobs):
            jobs_item = JobSummary.from_dict(jobs_item_data)



            jobs.append(jobs_item)


        total = d.pop("total")

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        jobs_response = cls(
            jobs=jobs,
            total=total,
            next_cursor=next_cursor,
        )

        return jobs_response
