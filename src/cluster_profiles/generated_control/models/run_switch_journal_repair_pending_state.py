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
  from ..models.run_switch_cancellation import RunSwitchCancellation





T = TypeVar("T", bound="RunSwitchJournalRepairPendingState")



@_attrs_define
class RunSwitchJournalRepairPendingState:
    """ Durable observation and cancellation while Job.result remains untouched.

        Attributes:
            deadline_at (datetime.datetime):
            next_attempt_at (datetime.datetime):
            attempts (int | Unset):  Default: 0.
            cancellation (None | RunSwitchCancellation | Unset):
     """

    deadline_at: datetime.datetime
    next_attempt_at: datetime.datetime
    attempts: int | Unset = 0
    cancellation: None | RunSwitchCancellation | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_cancellation import RunSwitchCancellation # noqa: PLC0415
        deadline_at = self.deadline_at.isoformat()

        next_attempt_at = self.next_attempt_at.isoformat()

        attempts = self.attempts

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, RunSwitchCancellation):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "deadline_at": deadline_at,
            "next_attempt_at": next_attempt_at,
        })
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_cancellation import RunSwitchCancellation # noqa: PLC0415
        d = dict(src_dict)
        deadline_at = datetime.datetime.fromisoformat(d.pop("deadline_at"))




        next_attempt_at = datetime.datetime.fromisoformat(d.pop("next_attempt_at"))




        attempts = d.pop("attempts", UNSET)

        def _parse_cancellation(data: object) -> None | RunSwitchCancellation | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = RunSwitchCancellation.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchCancellation | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        run_switch_journal_repair_pending_state = cls(
            deadline_at=deadline_at,
            next_attempt_at=next_attempt_at,
            attempts=attempts,
            cancellation=cancellation,
        )

        return run_switch_journal_repair_pending_state
