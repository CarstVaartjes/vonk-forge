from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_job_evidence import RecipeJobEvidence
  from ..models.failure_diagnostics import FailureDiagnostics
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
            schema_version (Literal[1]):
            diagnostics (Union['FailureDiagnostics', None, Unset]):
            reason (Union[None, Unset, str]):
     """

    evidence: 'RecipeJobEvidence'
    exit_code: int
    job_id: str
    output_manifest: 'RecipeJobOutputManifest'
    run_id: str
    schema_version: Literal[1]
    diagnostics: Union['FailureDiagnostics', None, Unset] = UNSET
    reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_evidence import RecipeJobEvidence
        from ..models.failure_diagnostics import FailureDiagnostics
        from ..models.recipe_job_output_manifest import RecipeJobOutputManifest
        evidence = self.evidence.to_dict()

        exit_code = self.exit_code

        job_id = self.job_id

        output_manifest = self.output_manifest.to_dict()

        run_id = self.run_id

        schema_version = self.schema_version

        diagnostics: Union[None, Unset, dict[str, Any]]
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, FailureDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        reason: Union[None, Unset, str]
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
            "schema_version": schema_version,
        })
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_evidence import RecipeJobEvidence
        from ..models.failure_diagnostics import FailureDiagnostics
        from ..models.recipe_job_output_manifest import RecipeJobOutputManifest
        d = dict(src_dict)
        evidence = RecipeJobEvidence.from_dict(d.pop("evidence"))




        exit_code = d.pop("exit_code")

        job_id = d.pop("job_id")

        output_manifest = RecipeJobOutputManifest.from_dict(d.pop("output_manifest"))




        run_id = d.pop("run_id")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_diagnostics(data: object) -> Union['FailureDiagnostics', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return diagnostics_type_0
            except: # noqa: E722
                pass
            return cast(Union['FailureDiagnostics', None, Unset], data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        recipe_job_run_result = cls(
            evidence=evidence,
            exit_code=exit_code,
            job_id=job_id,
            output_manifest=output_manifest,
            run_id=run_id,
            schema_version=schema_version,
            diagnostics=diagnostics,
            reason=reason,
        )

        return recipe_job_run_result
