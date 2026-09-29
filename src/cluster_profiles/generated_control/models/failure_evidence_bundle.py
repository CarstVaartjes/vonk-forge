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
  from ..models.operation_blocker import OperationBlocker





T = TypeVar("T", bound="FailureEvidenceBundle")



@_attrs_define
class FailureEvidenceBundle:
    """
        Attributes:
            collected_at (str):
            collector_errors (list[str]):
            context (EvidenceContext):
            diagnostics (FailureDiagnostics):
            error_code (str):
            summary (str):
            blockers (list[OperationBlocker] | Unset):
            detail (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    collected_at: str
    collector_errors: list[str]
    context: EvidenceContext
    diagnostics: FailureDiagnostics
    error_code: str
    summary: str
    blockers: list[OperationBlocker] | Unset = UNSET
    detail: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_context import EvidenceContext # noqa: PLC0415
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        collected_at = self.collected_at

        collector_errors = self.collector_errors



        context = self.context.to_dict()

        diagnostics = self.diagnostics.to_dict()

        error_code = self.error_code

        summary = self.summary

        blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "collected_at": collected_at,
            "collector_errors": collector_errors,
            "context": context,
            "diagnostics": diagnostics,
            "error_code": error_code,
            "summary": summary,
        })
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if detail is not UNSET:
            field_dict["detail"] = detail
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_context import EvidenceContext # noqa: PLC0415
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        d = dict(src_dict)
        collected_at = d.pop("collected_at")

        collector_errors = cast(list[str], d.pop("collector_errors"))


        context = EvidenceContext.from_dict(d.pop("context"))




        diagnostics = FailureDiagnostics.from_dict(d.pop("diagnostics"))




        error_code = d.pop("error_code")

        summary = d.pop("summary")

        _blockers = d.pop("blockers", UNSET)
        blockers: list[OperationBlocker] | Unset = UNSET
        if _blockers is not UNSET:
            blockers = []
            for blockers_item_data in _blockers:
                blockers_item = OperationBlocker.from_dict(blockers_item_data)



                blockers.append(blockers_item)


        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        failure_evidence_bundle = cls(
            collected_at=collected_at,
            collector_errors=collector_errors,
            context=context,
            diagnostics=diagnostics,
            error_code=error_code,
            summary=summary,
            blockers=blockers,
            detail=detail,
            schema_version=schema_version,
        )

        return failure_evidence_bundle
