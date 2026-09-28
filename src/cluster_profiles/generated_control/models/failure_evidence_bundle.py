from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.evidence_context import EvidenceContext
  from ..models.failure_diagnostics import FailureDiagnostics
  from ..models.operation_failure_evidence import OperationFailureEvidence





T = TypeVar("T", bound="FailureEvidenceBundle")



@_attrs_define
class FailureEvidenceBundle:
    """
        Attributes:
            collected_at (str):
            collector_errors (list[str]):
            context (EvidenceContext):
            diagnostics (FailureDiagnostics):
            receipt (OperationFailureEvidence): Small, sanitized operator evidence safe to expose in status responses.
            summary (str):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    collected_at: str
    collector_errors: list[str]
    context: EvidenceContext
    diagnostics: FailureDiagnostics
    receipt: OperationFailureEvidence
    summary: str
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_context import EvidenceContext # noqa: PLC0415
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        collected_at = self.collected_at

        collector_errors = self.collector_errors



        context = self.context.to_dict()

        diagnostics = self.diagnostics.to_dict()

        receipt = self.receipt.to_dict()

        summary = self.summary

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "collected_at": collected_at,
            "collector_errors": collector_errors,
            "context": context,
            "diagnostics": diagnostics,
            "receipt": receipt,
            "summary": summary,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_context import EvidenceContext # noqa: PLC0415
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.operation_failure_evidence import OperationFailureEvidence # noqa: PLC0415
        d = dict(src_dict)
        collected_at = d.pop("collected_at")

        collector_errors = cast(list[str], d.pop("collector_errors"))


        context = EvidenceContext.from_dict(d.pop("context"))




        diagnostics = FailureDiagnostics.from_dict(d.pop("diagnostics"))




        receipt = OperationFailureEvidence.from_dict(d.pop("receipt"))




        summary = d.pop("summary")

        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        failure_evidence_bundle = cls(
            collected_at=collected_at,
            collector_errors=collector_errors,
            context=context,
            diagnostics=diagnostics,
            receipt=receipt,
            summary=summary,
            schema_version=schema_version,
        )

        return failure_evidence_bundle
