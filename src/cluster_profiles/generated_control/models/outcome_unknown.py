from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.wait_reason import check_wait_reason
from ..models.wait_reason import WaitReason
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.outcome_evidence import OutcomeEvidence
  from ..models.recipe_job_run_result import RecipeJobRunResult





T = TypeVar("T", bound="OutcomeUnknown")



@_attrs_define
class OutcomeUnknown:
    """ The executor could not establish the effect.

        Attributes:
            kind (Literal['unknown']):
            reason (str):
            wait_reason (WaitReason): Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

                Each code names the one fact the executor could not establish.  The free
                text of a report is for people; the Controller decides on this code.
            evidence (None | OutcomeEvidence | Unset):
            receipt (None | RecipeJobRunResult | Unset):
            retry_after_seconds (int | None | Unset):
     """

    kind: Literal['unknown']
    reason: str
    wait_reason: WaitReason
    evidence: None | OutcomeEvidence | Unset = UNSET
    receipt: None | RecipeJobRunResult | Unset = UNSET
    retry_after_seconds: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.outcome_evidence import OutcomeEvidence # noqa: PLC0415
        from ..models.recipe_job_run_result import RecipeJobRunResult # noqa: PLC0415
        kind = self.kind

        reason = self.reason

        wait_reason: str = self.wait_reason

        evidence: dict[str, Any] | None | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        elif isinstance(self.evidence, OutcomeEvidence):
            evidence = self.evidence.to_dict()
        else:
            evidence = self.evidence

        receipt: dict[str, Any] | None | Unset
        if isinstance(self.receipt, Unset):
            receipt = UNSET
        elif isinstance(self.receipt, RecipeJobRunResult):
            receipt = self.receipt.to_dict()
        else:
            receipt = self.receipt

        retry_after_seconds: int | None | Unset
        if isinstance(self.retry_after_seconds, Unset):
            retry_after_seconds = UNSET
        else:
            retry_after_seconds = self.retry_after_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "reason": reason,
            "wait_reason": wait_reason,
        })
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if receipt is not UNSET:
            field_dict["receipt"] = receipt
        if retry_after_seconds is not UNSET:
            field_dict["retry_after_seconds"] = retry_after_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.outcome_evidence import OutcomeEvidence # noqa: PLC0415
        from ..models.recipe_job_run_result import RecipeJobRunResult # noqa: PLC0415
        d = dict(src_dict)
        kind = cast(Literal['unknown'] , d.pop("kind"))
        if kind != 'unknown':
            raise ValueError(f"kind must match const 'unknown', got '{kind}'")

        reason = d.pop("reason")

        wait_reason = check_wait_reason(d.pop("wait_reason"))




        def _parse_evidence(data: object) -> None | OutcomeEvidence | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_type_0 = OutcomeEvidence.from_dict(data)



                return evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OutcomeEvidence | Unset, data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))


        def _parse_receipt(data: object) -> None | RecipeJobRunResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receipt_type_0 = RecipeJobRunResult.from_dict(data)



                return receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeJobRunResult | Unset, data)

        receipt = _parse_receipt(d.pop("receipt", UNSET))


        def _parse_retry_after_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        retry_after_seconds = _parse_retry_after_seconds(d.pop("retry_after_seconds", UNSET))


        outcome_unknown = cls(
            kind=kind,
            reason=reason,
            wait_reason=wait_reason,
            evidence=evidence,
            receipt=receipt,
            retry_after_seconds=retry_after_seconds,
        )

        return outcome_unknown
