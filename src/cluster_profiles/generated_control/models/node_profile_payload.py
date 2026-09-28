from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="NodeProfilePayload")



@_attrs_define
class NodeProfilePayload:
    """
        Attributes:
            node_id (str):
            display_name_changed (bool | None | Unset):
            profile_changed (bool | None | Unset):
            schema_version (Literal[1] | Unset):  Default: 1.
     """

    node_id: str
    display_name_changed: bool | None | Unset = UNSET
    profile_changed: bool | None | Unset = UNSET
    schema_version: Literal[1] | Unset = 1





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        display_name_changed: bool | None | Unset
        if isinstance(self.display_name_changed, Unset):
            display_name_changed = UNSET
        else:
            display_name_changed = self.display_name_changed

        profile_changed: bool | None | Unset
        if isinstance(self.profile_changed, Unset):
            profile_changed = UNSET
        else:
            profile_changed = self.profile_changed

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
        })
        if display_name_changed is not UNSET:
            field_dict["display_name_changed"] = display_name_changed
        if profile_changed is not UNSET:
            field_dict["profile_changed"] = profile_changed
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        def _parse_display_name_changed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        display_name_changed = _parse_display_name_changed(d.pop("display_name_changed", UNSET))


        def _parse_profile_changed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        profile_changed = _parse_profile_changed(d.pop("profile_changed", UNSET))


        schema_version = cast(Literal[1] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        node_profile_payload = cls(
            node_id=node_id,
            display_name_changed=display_name_changed,
            profile_changed=profile_changed,
            schema_version=schema_version,
        )

        return node_profile_payload
