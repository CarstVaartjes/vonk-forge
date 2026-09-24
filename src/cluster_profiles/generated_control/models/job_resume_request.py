from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.job_resume_request_disposition import check_job_resume_request_disposition
from ..models.job_resume_request_disposition import JobResumeRequestDisposition
from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="JobResumeRequest")



@_attrs_define
class JobResumeRequest:
    """ What the operator wants the parked job's bounded authorisation to do.

    ``resume`` is the default and preserves the historic request shape: an
    omitted body or an omitted field authorises one more claim.  ``retire`` is
    the terminal disposition for work whose retry budget is already spent.

        Attributes:
            disposition (Union[Unset, JobResumeRequestDisposition]):  Default: 'resume'.
     """

    disposition: Union[Unset, JobResumeRequestDisposition] = 'resume'





    def to_dict(self) -> dict[str, Any]:
        disposition: Union[Unset, str] = UNSET
        if not isinstance(self.disposition, Unset):
            disposition = self.disposition



        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if disposition is not UNSET:
            field_dict["disposition"] = disposition

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _disposition = d.pop("disposition", UNSET)
        disposition: Union[Unset, JobResumeRequestDisposition]
        if isinstance(_disposition,  Unset):
            disposition = UNSET
        else:
            disposition = check_job_resume_request_disposition(_disposition)




        job_resume_request = cls(
            disposition=disposition,
        )

        return job_resume_request
