from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ManagedCatalogSyncRequest")



@_attrs_define
class ManagedCatalogSyncRequest:
    """
        Attributes:
            expected_commit (None | str | Unset):
            request_key (str | Unset):
     """

    expected_commit: None | str | Unset = UNSET
    request_key: str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        expected_commit: None | str | Unset
        if isinstance(self.expected_commit, Unset):
            expected_commit = UNSET
        else:
            expected_commit = self.expected_commit

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if expected_commit is not UNSET:
            field_dict["expected_commit"] = expected_commit
        if request_key is not UNSET:
            field_dict["request_key"] = request_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_expected_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_commit = _parse_expected_commit(d.pop("expected_commit", UNSET))


        request_key = d.pop("request_key", UNSET)

        managed_catalog_sync_request = cls(
            expected_commit=expected_commit,
            request_key=request_key,
        )

        return managed_catalog_sync_request
