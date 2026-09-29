from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchTargetTransferEvidenceResult")



@_attrs_define
class RunSwitchTargetTransferEvidenceResult:
    """
        Attributes:
            node_id (str):
            phase (Literal['transfer']):
            subphase (Literal['target-copy']):
            copied_bytes (int | None | Unset):
            downloaded_bytes (int | None | Unset):
     """

    node_id: str
    phase: Literal['transfer']
    subphase: Literal['target-copy']
    copied_bytes: int | None | Unset = UNSET
    downloaded_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        phase = self.phase

        subphase = self.subphase

        copied_bytes: int | None | Unset
        if isinstance(self.copied_bytes, Unset):
            copied_bytes = UNSET
        else:
            copied_bytes = self.copied_bytes

        downloaded_bytes: int | None | Unset
        if isinstance(self.downloaded_bytes, Unset):
            downloaded_bytes = UNSET
        else:
            downloaded_bytes = self.downloaded_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "phase": phase,
            "subphase": subphase,
        })
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if downloaded_bytes is not UNSET:
            field_dict["downloaded_bytes"] = downloaded_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        def _parse_copied_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        copied_bytes = _parse_copied_bytes(d.pop("copied_bytes", UNSET))


        def _parse_downloaded_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        downloaded_bytes = _parse_downloaded_bytes(d.pop("downloaded_bytes", UNSET))


        run_switch_target_transfer_evidence_result = cls(
            node_id=node_id,
            phase=phase,
            subphase=subphase,
            copied_bytes=copied_bytes,
            downloaded_bytes=downloaded_bytes,
        )

        return run_switch_target_transfer_evidence_result
