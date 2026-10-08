from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.journal_repair_purpose import check_journal_repair_purpose
from ..models.journal_repair_purpose import JournalRepairPurpose
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.native_progress_witness import NativeProgressWitness





T = TypeVar("T", bound="RunSwitchJournalRepairEvidence")



@_attrs_define
class RunSwitchJournalRepairEvidence:
    """ Historical evidence only: no runnable state, clock, or alternate intent.

        Attributes:
            algorithm (Literal['zero-transfer-native-install-v1']):
            corrected_digest (str):
            native_samples (list[NativeProgressWitness]):
            operation_id (str):
            original_digest (str):
            original_document (str):
            payload_digest (str):
            plan_digest (str):
            purpose (JournalRepairPurpose):
            recorded_at (datetime.datetime):
            request_key (str):
     """

    algorithm: Literal['zero-transfer-native-install-v1']
    corrected_digest: str
    native_samples: list[NativeProgressWitness]
    operation_id: str
    original_digest: str
    original_document: str
    payload_digest: str
    plan_digest: str
    purpose: JournalRepairPurpose
    recorded_at: datetime.datetime
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.native_progress_witness import NativeProgressWitness # noqa: PLC0415
        algorithm = self.algorithm

        corrected_digest = self.corrected_digest

        native_samples = []
        for native_samples_item_data in self.native_samples:
            native_samples_item = native_samples_item_data.to_dict()
            native_samples.append(native_samples_item)



        operation_id = self.operation_id

        original_digest = self.original_digest

        original_document = self.original_document

        payload_digest = self.payload_digest

        plan_digest = self.plan_digest

        purpose: str = self.purpose

        recorded_at = self.recorded_at.isoformat()

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "algorithm": algorithm,
            "corrected_digest": corrected_digest,
            "native_samples": native_samples,
            "operation_id": operation_id,
            "original_digest": original_digest,
            "original_document": original_document,
            "payload_digest": payload_digest,
            "plan_digest": plan_digest,
            "purpose": purpose,
            "recorded_at": recorded_at,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.native_progress_witness import NativeProgressWitness # noqa: PLC0415
        d = dict(src_dict)
        algorithm = cast(Literal['zero-transfer-native-install-v1'] , d.pop("algorithm"))
        if algorithm != 'zero-transfer-native-install-v1':
            raise ValueError(f"algorithm must match const 'zero-transfer-native-install-v1', got '{algorithm}'")

        corrected_digest = d.pop("corrected_digest")

        native_samples = []
        _native_samples = d.pop("native_samples")
        for native_samples_item_data in (_native_samples):
            native_samples_item = NativeProgressWitness.from_dict(native_samples_item_data)



            native_samples.append(native_samples_item)


        operation_id = d.pop("operation_id")

        original_digest = d.pop("original_digest")

        original_document = d.pop("original_document")

        payload_digest = d.pop("payload_digest")

        plan_digest = d.pop("plan_digest")

        purpose = check_journal_repair_purpose(d.pop("purpose"))




        recorded_at = datetime.datetime.fromisoformat(d.pop("recorded_at"))




        request_key = d.pop("request_key")

        run_switch_journal_repair_evidence = cls(
            algorithm=algorithm,
            corrected_digest=corrected_digest,
            native_samples=native_samples,
            operation_id=operation_id,
            original_digest=original_digest,
            original_document=original_document,
            payload_digest=payload_digest,
            plan_digest=plan_digest,
            purpose=purpose,
            recorded_at=recorded_at,
            request_key=request_key,
        )

        return run_switch_journal_repair_evidence
