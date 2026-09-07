from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_upstream_revision_status import check_model_cache_upstream_revision_status
from ..models.model_cache_upstream_revision_status import ModelCacheUpstreamRevisionStatus
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ModelCacheUpstreamRevision")



@_attrs_define
class ModelCacheUpstreamRevision:
    """
        Attributes:
            checked_at (str):
            pinned_revision (str):
            repository (str):
            status (ModelCacheUpstreamRevisionStatus):
            error_code (Union[None, Unset, str]):
            latest_revision (Union[None, Unset, str]):
     """

    checked_at: str
    pinned_revision: str
    repository: str
    status: ModelCacheUpstreamRevisionStatus
    error_code: Union[None, Unset, str] = UNSET
    latest_revision: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        checked_at = self.checked_at

        pinned_revision = self.pinned_revision

        repository = self.repository

        status: str = self.status

        error_code: Union[None, Unset, str]
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        latest_revision: Union[None, Unset, str]
        if isinstance(self.latest_revision, Unset):
            latest_revision = UNSET
        else:
            latest_revision = self.latest_revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "checked_at": checked_at,
            "pinned_revision": pinned_revision,
            "repository": repository,
            "status": status,
        })
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if latest_revision is not UNSET:
            field_dict["latest_revision"] = latest_revision

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        checked_at = d.pop("checked_at")

        pinned_revision = d.pop("pinned_revision")

        repository = d.pop("repository")

        status = check_model_cache_upstream_revision_status(d.pop("status"))




        def _parse_error_code(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))


        def _parse_latest_revision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        latest_revision = _parse_latest_revision(d.pop("latest_revision", UNSET))


        model_cache_upstream_revision = cls(
            checked_at=checked_at,
            pinned_revision=pinned_revision,
            repository=repository,
            status=status,
            error_code=error_code,
            latest_revision=latest_revision,
        )

        return model_cache_upstream_revision
