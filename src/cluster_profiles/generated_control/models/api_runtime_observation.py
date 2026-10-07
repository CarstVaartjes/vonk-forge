from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ApiRuntimeObservation")



@_attrs_define
class ApiRuntimeObservation:
    """
        Attributes:
            control_contract_sha256 (None | str):
            source_sha (None | str):
     """

    control_contract_sha256: None | str
    source_sha: None | str





    def to_dict(self) -> dict[str, Any]:
        control_contract_sha256: None | str
        control_contract_sha256 = self.control_contract_sha256

        source_sha: None | str
        source_sha = self.source_sha


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "control_contract_sha256": control_contract_sha256,
            "source_sha": source_sha,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_control_contract_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        control_contract_sha256 = _parse_control_contract_sha256(d.pop("control_contract_sha256"))


        def _parse_source_sha(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        source_sha = _parse_source_sha(d.pop("source_sha"))


        api_runtime_observation = cls(
            control_contract_sha256=control_contract_sha256,
            source_sha=source_sha,
        )

        return api_runtime_observation
