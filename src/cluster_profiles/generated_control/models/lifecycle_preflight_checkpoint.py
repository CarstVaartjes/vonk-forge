from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts
  from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts





T = TypeVar("T", bound="LifecyclePreflightCheckpoint")



@_attrs_define
class LifecyclePreflightCheckpoint:
    """
        Attributes:
            phase_index (int):
            attempts (LifecyclePreflightCheckpointAttempts | Unset):
            last_failure_code (None | str | Unset):
            last_failure_detail (None | str | Unset):
            next_check_at (datetime.datetime | None | Unset):
            pending_job_id (None | str | Unset):
            pending_node_id (None | str | Unset):
            receipts (LifecyclePreflightCheckpointReceipts | Unset):
     """

    phase_index: int
    attempts: LifecyclePreflightCheckpointAttempts | Unset = UNSET
    last_failure_code: None | str | Unset = UNSET
    last_failure_detail: None | str | Unset = UNSET
    next_check_at: datetime.datetime | None | Unset = UNSET
    pending_job_id: None | str | Unset = UNSET
    pending_node_id: None | str | Unset = UNSET
    receipts: LifecyclePreflightCheckpointReceipts | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts # noqa: PLC0415
        from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts # noqa: PLC0415
        phase_index = self.phase_index

        attempts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.attempts, Unset):
            attempts = self.attempts.to_dict()

        last_failure_code: None | str | Unset
        if isinstance(self.last_failure_code, Unset):
            last_failure_code = UNSET
        else:
            last_failure_code = self.last_failure_code

        last_failure_detail: None | str | Unset
        if isinstance(self.last_failure_detail, Unset):
            last_failure_detail = UNSET
        else:
            last_failure_detail = self.last_failure_detail

        next_check_at: None | str | Unset
        if isinstance(self.next_check_at, Unset):
            next_check_at = UNSET
        elif isinstance(self.next_check_at, datetime.datetime):
            next_check_at = self.next_check_at.isoformat()
        else:
            next_check_at = self.next_check_at

        pending_job_id: None | str | Unset
        if isinstance(self.pending_job_id, Unset):
            pending_job_id = UNSET
        else:
            pending_job_id = self.pending_job_id

        pending_node_id: None | str | Unset
        if isinstance(self.pending_node_id, Unset):
            pending_node_id = UNSET
        else:
            pending_node_id = self.pending_node_id

        receipts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.receipts, Unset):
            receipts = self.receipts.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase_index": phase_index,
        })
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if last_failure_code is not UNSET:
            field_dict["last_failure_code"] = last_failure_code
        if last_failure_detail is not UNSET:
            field_dict["last_failure_detail"] = last_failure_detail
        if next_check_at is not UNSET:
            field_dict["next_check_at"] = next_check_at
        if pending_job_id is not UNSET:
            field_dict["pending_job_id"] = pending_job_id
        if pending_node_id is not UNSET:
            field_dict["pending_node_id"] = pending_node_id
        if receipts is not UNSET:
            field_dict["receipts"] = receipts

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts # noqa: PLC0415
        from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts # noqa: PLC0415
        d = dict(src_dict)
        phase_index = d.pop("phase_index")

        _attempts = d.pop("attempts", UNSET)
        attempts: LifecyclePreflightCheckpointAttempts | Unset
        if isinstance(_attempts,  Unset):
            attempts = UNSET
        else:
            attempts = LifecyclePreflightCheckpointAttempts.from_dict(_attempts)




        def _parse_last_failure_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_failure_code = _parse_last_failure_code(d.pop("last_failure_code", UNSET))


        def _parse_last_failure_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_failure_detail = _parse_last_failure_detail(d.pop("last_failure_detail", UNSET))


        def _parse_next_check_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_check_at_type_0 = datetime.datetime.fromisoformat(data)



                return next_check_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        next_check_at = _parse_next_check_at(d.pop("next_check_at", UNSET))


        def _parse_pending_job_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pending_job_id = _parse_pending_job_id(d.pop("pending_job_id", UNSET))


        def _parse_pending_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pending_node_id = _parse_pending_node_id(d.pop("pending_node_id", UNSET))


        _receipts = d.pop("receipts", UNSET)
        receipts: LifecyclePreflightCheckpointReceipts | Unset
        if isinstance(_receipts,  Unset):
            receipts = UNSET
        else:
            receipts = LifecyclePreflightCheckpointReceipts.from_dict(_receipts)




        lifecycle_preflight_checkpoint = cls(
            phase_index=phase_index,
            attempts=attempts,
            last_failure_code=last_failure_code,
            last_failure_detail=last_failure_detail,
            next_check_at=next_check_at,
            pending_job_id=pending_job_id,
            pending_node_id=pending_node_id,
            receipts=receipts,
        )

        return lifecycle_preflight_checkpoint
