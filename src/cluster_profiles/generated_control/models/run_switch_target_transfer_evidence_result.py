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
            diagnostic (None | str | Unset):
            downloaded_bytes (int | None | Unset):
            error (None | str | Unset):
            error_code (None | str | Unset):
            failure_kind (None | str | Unset):
            reason (None | str | Unset):
            uncertain (bool | Unset):  Default: False.
     """

    node_id: str
    phase: Literal['transfer']
    subphase: Literal['target-copy']
    copied_bytes: int | None | Unset = UNSET
    diagnostic: None | str | Unset = UNSET
    downloaded_bytes: int | None | Unset = UNSET
    error: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    failure_kind: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    uncertain: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        phase = self.phase

        subphase = self.subphase

        copied_bytes: int | None | Unset
        if isinstance(self.copied_bytes, Unset):
            copied_bytes = UNSET
        else:
            copied_bytes = self.copied_bytes

        diagnostic: None | str | Unset
        if isinstance(self.diagnostic, Unset):
            diagnostic = UNSET
        else:
            diagnostic = self.diagnostic

        downloaded_bytes: int | None | Unset
        if isinstance(self.downloaded_bytes, Unset):
            downloaded_bytes = UNSET
        else:
            downloaded_bytes = self.downloaded_bytes

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        failure_kind: None | str | Unset
        if isinstance(self.failure_kind, Unset):
            failure_kind = UNSET
        else:
            failure_kind = self.failure_kind

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        uncertain = self.uncertain


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "phase": phase,
            "subphase": subphase,
        })
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if diagnostic is not UNSET:
            field_dict["diagnostic"] = diagnostic
        if downloaded_bytes is not UNSET:
            field_dict["downloaded_bytes"] = downloaded_bytes
        if error is not UNSET:
            field_dict["error"] = error
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if failure_kind is not UNSET:
            field_dict["failure_kind"] = failure_kind
        if reason is not UNSET:
            field_dict["reason"] = reason
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain

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


        def _parse_diagnostic(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        diagnostic = _parse_diagnostic(d.pop("diagnostic", UNSET))


        def _parse_downloaded_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        downloaded_bytes = _parse_downloaded_bytes(d.pop("downloaded_bytes", UNSET))


        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))


        def _parse_failure_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_kind = _parse_failure_kind(d.pop("failure_kind", UNSET))


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        run_switch_target_transfer_evidence_result = cls(
            node_id=node_id,
            phase=phase,
            subphase=subphase,
            copied_bytes=copied_bytes,
            diagnostic=diagnostic,
            downloaded_bytes=downloaded_bytes,
            error=error,
            error_code=error_code,
            failure_kind=failure_kind,
            reason=reason,
            uncertain=uncertain,
        )

        return run_switch_target_transfer_evidence_result
