from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.failure_diagnostics import FailureDiagnostics
  from ..models.recipe_job_evidence import RecipeJobEvidence
  from ..models.recipe_job_output_manifest import RecipeJobOutputManifest





T = TypeVar("T", bound="RecipeJobRunResult")



@_attrs_define
class RecipeJobRunResult:
    """
        Attributes:
            evidence (RecipeJobEvidence):
            exit_code (int):
            job_id (str):
            output_manifest (RecipeJobOutputManifest):
            run_id (str):
            diagnostics (FailureDiagnostics | None | Unset):
            reason (None | str | Unset):
     """

    evidence: RecipeJobEvidence
    exit_code: int
    job_id: str
    output_manifest: RecipeJobOutputManifest
    run_id: str
    diagnostics: FailureDiagnostics | None | Unset = UNSET
    reason: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.recipe_job_evidence import RecipeJobEvidence # noqa: PLC0415
        from ..models.recipe_job_output_manifest import RecipeJobOutputManifest # noqa: PLC0415
        evidence = self.evidence.to_dict()

        exit_code = self.exit_code

        job_id = self.job_id

        output_manifest = self.output_manifest.to_dict()

        run_id = self.run_id

        diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, FailureDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence": evidence,
            "exit_code": exit_code,
            "job_id": job_id,
            "output_manifest": output_manifest,
            "run_id": run_id,
        })
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.recipe_job_evidence import RecipeJobEvidence # noqa: PLC0415
        from ..models.recipe_job_output_manifest import RecipeJobOutputManifest # noqa: PLC0415
        d = dict(src_dict)
        evidence = RecipeJobEvidence.from_dict(d.pop("evidence"))




        exit_code = d.pop("exit_code")

        job_id = d.pop("job_id")

        output_manifest = RecipeJobOutputManifest.from_dict(d.pop("output_manifest"))




        run_id = d.pop("run_id")

        def _parse_diagnostics(data: object) -> FailureDiagnostics | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FailureDiagnostics | None | Unset, data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        recipe_job_run_result = cls(
            evidence=evidence,
            exit_code=exit_code,
            job_id=job_id,
            output_manifest=output_manifest,
            run_id=run_id,
            diagnostics=diagnostics,
            reason=reason,
        )

        return recipe_job_run_result
