from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.run_switch_cancellation import RunSwitchCancellation





T = TypeVar("T", bound="RunSwitchJournalRepairEndEvidence")



@_attrs_define
class RunSwitchJournalRepairEndEvidence:
    """ An ended observation retains the original journal and cancel request.

        Attributes:
            code (Literal['run-switch.journal-repair-exhausted']):
            operation_id (str):
            original_digest (str):
            original_document (str):
            recorded_at (datetime.datetime):
            request_key (str):
            cancellation (None | RunSwitchCancellation | Unset):
     """

    code: Literal['run-switch.journal-repair-exhausted']
    operation_id: str
    original_digest: str
    original_document: str
    recorded_at: datetime.datetime
    request_key: str
    cancellation: None | RunSwitchCancellation | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_cancellation import RunSwitchCancellation # noqa: PLC0415
        code = self.code

        operation_id = self.operation_id

        original_digest = self.original_digest

        original_document = self.original_document

        recorded_at = self.recorded_at.isoformat()

        request_key = self.request_key

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, RunSwitchCancellation):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "operation_id": operation_id,
            "original_digest": original_digest,
            "original_document": original_document,
            "recorded_at": recorded_at,
            "request_key": request_key,
        })
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_cancellation import RunSwitchCancellation # noqa: PLC0415
        d = dict(src_dict)
        code = cast(Literal['run-switch.journal-repair-exhausted'] , d.pop("code"))
        if code != 'run-switch.journal-repair-exhausted':
            raise ValueError(f"code must match const 'run-switch.journal-repair-exhausted', got '{code}'")

        operation_id = d.pop("operation_id")

        original_digest = d.pop("original_digest")

        original_document = d.pop("original_document")

        recorded_at = datetime.datetime.fromisoformat(d.pop("recorded_at"))




        request_key = d.pop("request_key")

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


        run_switch_journal_repair_end_evidence = cls(
            code=code,
            operation_id=operation_id,
            original_digest=original_digest,
            original_document=original_document,
            recorded_at=recorded_at,
            request_key=request_key,
            cancellation=cancellation,
        )

        return run_switch_journal_repair_end_evidence
