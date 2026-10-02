from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetProfileLoadRequest")



@_attrs_define
class FleetProfileLoadRequest:
    """
        Attributes:
            request_key (str):
            reviewed_effects_digest (None | str | Unset):
     """

    request_key: str
    reviewed_effects_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        reviewed_effects_digest: None | str | Unset
        if isinstance(self.reviewed_effects_digest, Unset):
            reviewed_effects_digest = UNSET
        else:
            reviewed_effects_digest = self.reviewed_effects_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if reviewed_effects_digest is not UNSET:
            field_dict["reviewed_effects_digest"] = reviewed_effects_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        def _parse_reviewed_effects_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reviewed_effects_digest = _parse_reviewed_effects_digest(d.pop("reviewed_effects_digest", UNSET))


        fleet_profile_load_request = cls(
            request_key=request_key,
            reviewed_effects_digest=reviewed_effects_digest,
        )

        return fleet_profile_load_request
