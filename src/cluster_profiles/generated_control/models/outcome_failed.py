from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_failure_kind import AgentFailureKind
from ..models.agent_failure_kind import check_agent_failure_kind
from ..models.failure_code import check_failure_code
from ..models.failure_code import FailureCode
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.outcome_evidence import OutcomeEvidence
  from ..models.recipe_job_run_result import RecipeJobRunResult





T = TypeVar("T", bound="OutcomeFailed")



@_attrs_define
class OutcomeFailed:
    """ A definite failure.

    ``receipt`` is the process receipt of a one-shot recipe job whose process
    ran and exited nonzero; every other failure reports ``code`` and ``reason``.

        Attributes:
            code (FailureCode): Closed codes of a definite failed outcome reported by the agent.
            kind (Literal['failed']):
            reason (str):
            evidence (None | OutcomeEvidence | Unset):
            failure_kind (AgentFailureKind | None | Unset):
            receipt (None | RecipeJobRunResult | Unset):
            retry_after_seconds (int | None | Unset):
     """

    code: FailureCode
    kind: Literal['failed']
    reason: str
    evidence: None | OutcomeEvidence | Unset = UNSET
    failure_kind: AgentFailureKind | None | Unset = UNSET
    receipt: None | RecipeJobRunResult | Unset = UNSET
    retry_after_seconds: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.outcome_evidence import OutcomeEvidence # noqa: PLC0415
        from ..models.recipe_job_run_result import RecipeJobRunResult # noqa: PLC0415
        code: str = self.code

        kind = self.kind

        reason = self.reason

        evidence: dict[str, Any] | None | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        elif isinstance(self.evidence, OutcomeEvidence):
            evidence = self.evidence.to_dict()
        else:
            evidence = self.evidence

        failure_kind: None | str | Unset
        if isinstance(self.failure_kind, Unset):
            failure_kind = UNSET
        elif isinstance(self.failure_kind, str):
            failure_kind = self.failure_kind
        else:
            failure_kind = self.failure_kind

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
            "code": code,
            "kind": kind,
            "reason": reason,
        })
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if failure_kind is not UNSET:
            field_dict["failure_kind"] = failure_kind
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
        code = check_failure_code(d.pop("code"))




        kind = cast(Literal['failed'] , d.pop("kind"))
        if kind != 'failed':
            raise ValueError(f"kind must match const 'failed', got '{kind}'")

        reason = d.pop("reason")

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


        def _parse_failure_kind(data: object) -> AgentFailureKind | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                failure_kind_type_0 = check_agent_failure_kind(data)



                return failure_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentFailureKind | None | Unset, data)

        failure_kind = _parse_failure_kind(d.pop("failure_kind", UNSET))


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


        outcome_failed = cls(
            code=code,
            kind=kind,
            reason=reason,
            evidence=evidence,
            failure_kind=failure_kind,
            receipt=receipt,
            retry_after_seconds=retry_after_seconds,
        )

        return outcome_failed
