from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.agent_install_result import AgentInstallResult
  from ..models.agent_upgrade_result import AgentUpgradeResult
  from ..models.artifact_distribution_result import ArtifactDistributionResult
  from ..models.recipe_build_cleanup_evidence import RecipeBuildCleanupEvidence
  from ..models.recipe_build_evidence import RecipeBuildEvidence
  from ..models.recipe_job_run_result import RecipeJobRunResult
  from ..models.recipe_reconcile_result import RecipeReconcileResult
  from ..models.recipe_start_result import RecipeStartResult
  from ..models.recipe_stop_result import RecipeStopResult
  from ..models.recipe_uninstall_result import RecipeUninstallResult
  from ..models.runtime_preflight_result import RuntimePreflightResult





T = TypeVar("T", bound="OutcomeDone")



@_attrs_define
class OutcomeDone:
    """ The effect is established; ``result`` is the operation's success body.

        Attributes:
            kind (Literal['done']):
            result (AgentInstallResult | AgentUpgradeResult | ArtifactDistributionResult | RecipeBuildCleanupEvidence |
                RecipeBuildEvidence | RecipeJobRunResult | RecipeReconcileResult | RecipeStartResult | RecipeStopResult |
                RecipeUninstallResult | RuntimePreflightResult):
     """

    kind: Literal['done']
    result: AgentInstallResult | AgentUpgradeResult | ArtifactDistributionResult | RecipeBuildCleanupEvidence | RecipeBuildEvidence | RecipeJobRunResult | RecipeReconcileResult | RecipeStartResult | RecipeStopResult | RecipeUninstallResult | RuntimePreflightResult





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_install_result import AgentInstallResult # noqa: PLC0415
        from ..models.agent_upgrade_result import AgentUpgradeResult # noqa: PLC0415
        from ..models.artifact_distribution_result import ArtifactDistributionResult # noqa: PLC0415
        from ..models.recipe_build_cleanup_evidence import RecipeBuildCleanupEvidence # noqa: PLC0415
        from ..models.recipe_build_evidence import RecipeBuildEvidence # noqa: PLC0415
        from ..models.recipe_job_run_result import RecipeJobRunResult # noqa: PLC0415
        from ..models.recipe_reconcile_result import RecipeReconcileResult # noqa: PLC0415
        from ..models.recipe_start_result import RecipeStartResult # noqa: PLC0415
        from ..models.recipe_stop_result import RecipeStopResult # noqa: PLC0415
        from ..models.recipe_uninstall_result import RecipeUninstallResult # noqa: PLC0415
        from ..models.runtime_preflight_result import RuntimePreflightResult # noqa: PLC0415
        kind = self.kind

        result: dict[str, Any]
        if isinstance(self.result, RuntimePreflightResult):
            result = self.result.to_dict()
        elif isinstance(self.result, AgentInstallResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeStartResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeStopResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeReconcileResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeUninstallResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeBuildEvidence):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeBuildCleanupEvidence):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeJobRunResult):
            result = self.result.to_dict()
        elif isinstance(self.result, ArtifactDistributionResult):
            result = self.result.to_dict()
        else:
            result = self.result.to_dict()



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "result": result,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_install_result import AgentInstallResult # noqa: PLC0415
        from ..models.agent_upgrade_result import AgentUpgradeResult # noqa: PLC0415
        from ..models.artifact_distribution_result import ArtifactDistributionResult # noqa: PLC0415
        from ..models.recipe_build_cleanup_evidence import RecipeBuildCleanupEvidence # noqa: PLC0415
        from ..models.recipe_build_evidence import RecipeBuildEvidence # noqa: PLC0415
        from ..models.recipe_job_run_result import RecipeJobRunResult # noqa: PLC0415
        from ..models.recipe_reconcile_result import RecipeReconcileResult # noqa: PLC0415
        from ..models.recipe_start_result import RecipeStartResult # noqa: PLC0415
        from ..models.recipe_stop_result import RecipeStopResult # noqa: PLC0415
        from ..models.recipe_uninstall_result import RecipeUninstallResult # noqa: PLC0415
        from ..models.runtime_preflight_result import RuntimePreflightResult # noqa: PLC0415
        d = dict(src_dict)
        kind = cast(Literal['done'] , d.pop("kind"))
        if kind != 'done':
            raise ValueError(f"kind must match const 'done', got '{kind}'")

        def _parse_result(data: object) -> AgentInstallResult | AgentUpgradeResult | ArtifactDistributionResult | RecipeBuildCleanupEvidence | RecipeBuildEvidence | RecipeJobRunResult | RecipeReconcileResult | RecipeStartResult | RecipeStopResult | RecipeUninstallResult | RuntimePreflightResult:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = RuntimePreflightResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = AgentInstallResult.from_dict(data)



                return result_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_2 = RecipeStartResult.from_dict(data)



                return result_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_3 = RecipeStopResult.from_dict(data)



                return result_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_4 = RecipeReconcileResult.from_dict(data)



                return result_type_4
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_5 = RecipeUninstallResult.from_dict(data)



                return result_type_5
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_6 = RecipeBuildEvidence.from_dict(data)



                return result_type_6
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_7 = RecipeBuildCleanupEvidence.from_dict(data)



                return result_type_7
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_8 = RecipeJobRunResult.from_dict(data)



                return result_type_8
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_9 = ArtifactDistributionResult.from_dict(data)



                return result_type_9
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            result_type_10 = AgentUpgradeResult.from_dict(data)



            return result_type_10

        result = _parse_result(d.pop("result"))


        outcome_done = cls(
            kind=kind,
            result=result,
        )

        return outcome_done
