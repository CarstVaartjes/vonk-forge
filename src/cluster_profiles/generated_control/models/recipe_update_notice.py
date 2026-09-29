from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeUpdateNotice")



@_attrs_define
class RecipeUpdateNotice:
    """ A running workload uses an older revision than the newest active one.

        Attributes:
            detail (str):
            newest_revision_id (str):
            running_revision_id (str):
            code (str | Unset):  Default: 'recipe.update_available'.
            newest_released_at (None | str | Unset):
            newest_version (None | str | Unset):
            running_released_at (None | str | Unset):
            running_version (None | str | Unset):
            severity (str | Unset):  Default: 'info'.
     """

    detail: str
    newest_revision_id: str
    running_revision_id: str
    code: str | Unset = 'recipe.update_available'
    newest_released_at: None | str | Unset = UNSET
    newest_version: None | str | Unset = UNSET
    running_released_at: None | str | Unset = UNSET
    running_version: None | str | Unset = UNSET
    severity: str | Unset = 'info'





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        newest_revision_id = self.newest_revision_id

        running_revision_id = self.running_revision_id

        code = self.code

        newest_released_at: None | str | Unset
        if isinstance(self.newest_released_at, Unset):
            newest_released_at = UNSET
        else:
            newest_released_at = self.newest_released_at

        newest_version: None | str | Unset
        if isinstance(self.newest_version, Unset):
            newest_version = UNSET
        else:
            newest_version = self.newest_version

        running_released_at: None | str | Unset
        if isinstance(self.running_released_at, Unset):
            running_released_at = UNSET
        else:
            running_released_at = self.running_released_at

        running_version: None | str | Unset
        if isinstance(self.running_version, Unset):
            running_version = UNSET
        else:
            running_version = self.running_version

        severity = self.severity


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "newest_revision_id": newest_revision_id,
            "running_revision_id": running_revision_id,
        })
        if code is not UNSET:
            field_dict["code"] = code
        if newest_released_at is not UNSET:
            field_dict["newest_released_at"] = newest_released_at
        if newest_version is not UNSET:
            field_dict["newest_version"] = newest_version
        if running_released_at is not UNSET:
            field_dict["running_released_at"] = running_released_at
        if running_version is not UNSET:
            field_dict["running_version"] = running_version
        if severity is not UNSET:
            field_dict["severity"] = severity

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        newest_revision_id = d.pop("newest_revision_id")

        running_revision_id = d.pop("running_revision_id")

        code = d.pop("code", UNSET)

        def _parse_newest_released_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        newest_released_at = _parse_newest_released_at(d.pop("newest_released_at", UNSET))


        def _parse_newest_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        newest_version = _parse_newest_version(d.pop("newest_version", UNSET))


        def _parse_running_released_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        running_released_at = _parse_running_released_at(d.pop("running_released_at", UNSET))


        def _parse_running_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        running_version = _parse_running_version(d.pop("running_version", UNSET))


        severity = d.pop("severity", UNSET)

        recipe_update_notice = cls(
            detail=detail,
            newest_revision_id=newest_revision_id,
            running_revision_id=running_revision_id,
            code=code,
            newest_released_at=newest_released_at,
            newest_version=newest_version,
            running_released_at=running_released_at,
            running_version=running_version,
            severity=severity,
        )

        return recipe_update_notice
