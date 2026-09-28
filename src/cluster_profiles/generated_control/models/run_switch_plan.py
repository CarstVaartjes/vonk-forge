from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_plan_action import check_run_switch_plan_action
from ..models.run_switch_plan_action import RunSwitchPlanAction
from ..models.run_switch_plan_cleanup_disposition import check_run_switch_plan_cleanup_disposition
from ..models.run_switch_plan_cleanup_disposition import RunSwitchPlanCleanupDisposition
from ..models.run_switch_plan_cleanup_mode import check_run_switch_plan_cleanup_mode
from ..models.run_switch_plan_cleanup_mode import RunSwitchPlanCleanupMode
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.artifact_storage_impact import ArtifactStorageImpact
  from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck
  from ..models.effective_settings_selection import EffectiveSettingsSelection
  from ..models.freshness_evidence import FreshnessEvidence
  from ..models.invocation_metadata import InvocationMetadata
  from ..models.mapping_selection import MappingSelection
  from ..models.rollout_preparation import RolloutPreparation
  from ..models.run_switch_build_evidence import RunSwitchBuildEvidence
  from ..models.run_switch_phase import RunSwitchPhase
  from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope
  from ..models.run_switch_reason import RunSwitchReason
  from ..models.run_switch_reconciliation_authority import RunSwitchReconciliationAuthority
  from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
  from ..models.spark_fit import SparkFit
  from ..models.spark_group import SparkGroup
  from ..models.stop_impact import StopImpact





T = TypeVar("T", bound="RunSwitchPlan")



@_attrs_define
class RunSwitchPlan:
    """
        Attributes:
            action (RunSwitchPlanAction):
            alias (None | str):
            allowed (bool):
            blockers (list[RunSwitchReason]):
            build (RunSwitchBuildEvidence):
            conflicts (list[RunSwitchReason]):
            fit (SparkFit):
            fit_after_stop (None | SparkFit):
            fit_current (SparkFit):
            generated_at (datetime.datetime):
            image_digest (None | str):
            installation_id (None | str):
            installation_state (None | str):
            invocation (InvocationMetadata): Context for audit and tracing which has no decision-making authority.
            mapping (MappingSelection | None):
            model_content_sha256 (None | str):
            phases (list[RunSwitchPhase]):
            plan_digest (str):
            recipe_build_id (None | str):
            recipe_content_sha256 (None | str):
            recipe_revision_id (None | str):
            reclaimed_bytes (int):
            run_id (None | str):
            runtime_storage (RuntimeImageStorageImpact):
            spark_group (SparkGroup): A complete, rank-labelled Spark group selected by the operator.
            start_plan_digest (None | str):
            stops (list[StopImpact]):
            storage (ArtifactStorageImpact): Byte impact with unknown values preserved as unknown, never guessed.
            warnings (list[RunSwitchReason]):
            cleanup_disposition (RunSwitchPlanCleanupDisposition | Unset):  Default: 'uninstall'.
            cleanup_mode (RunSwitchPlanCleanupMode | Unset):  Default: 'uninstall'.
            effective_settings (EffectiveSettingsSelection | None | Unset):
            freshness (list[FreshnessEvidence] | Unset):
            post_stop_memory_check (ConditionalPostStopMemoryCheck | None | Unset):
            preparation (None | RolloutPreparation | Unset):
            profile_stop_scope (None | RunSwitchProfileStopScope | Unset):
            reconciliation_authority (None | RunSwitchReconciliationAuthority | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
            stop_before_prepare (bool | Unset):  Default: False.
            stop_before_transfer (bool | Unset):  Default: False.
     """

    action: RunSwitchPlanAction
    alias: None | str
    allowed: bool
    blockers: list[RunSwitchReason]
    build: RunSwitchBuildEvidence
    conflicts: list[RunSwitchReason]
    fit: SparkFit
    fit_after_stop: None | SparkFit
    fit_current: SparkFit
    generated_at: datetime.datetime
    image_digest: None | str
    installation_id: None | str
    installation_state: None | str
    invocation: InvocationMetadata
    mapping: MappingSelection | None
    model_content_sha256: None | str
    phases: list[RunSwitchPhase]
    plan_digest: str
    recipe_build_id: None | str
    recipe_content_sha256: None | str
    recipe_revision_id: None | str
    reclaimed_bytes: int
    run_id: None | str
    runtime_storage: RuntimeImageStorageImpact
    spark_group: SparkGroup
    start_plan_digest: None | str
    stops: list[StopImpact]
    storage: ArtifactStorageImpact
    warnings: list[RunSwitchReason]
    cleanup_disposition: RunSwitchPlanCleanupDisposition | Unset = 'uninstall'
    cleanup_mode: RunSwitchPlanCleanupMode | Unset = 'uninstall'
    effective_settings: EffectiveSettingsSelection | None | Unset = UNSET
    freshness: list[FreshnessEvidence] | Unset = UNSET
    post_stop_memory_check: ConditionalPostStopMemoryCheck | None | Unset = UNSET
    preparation: None | RolloutPreparation | Unset = UNSET
    profile_stop_scope: None | RunSwitchProfileStopScope | Unset = UNSET
    reconciliation_authority: None | RunSwitchReconciliationAuthority | Unset = UNSET
    schema_version: Literal[2] | Unset = 2
    stop_before_prepare: bool | Unset = False
    stop_before_transfer: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_storage_impact import ArtifactStorageImpact # noqa: PLC0415
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck # noqa: PLC0415
        from ..models.effective_settings_selection import EffectiveSettingsSelection # noqa: PLC0415
        from ..models.freshness_evidence import FreshnessEvidence # noqa: PLC0415
        from ..models.invocation_metadata import InvocationMetadata # noqa: PLC0415
        from ..models.mapping_selection import MappingSelection # noqa: PLC0415
        from ..models.rollout_preparation import RolloutPreparation # noqa: PLC0415
        from ..models.run_switch_build_evidence import RunSwitchBuildEvidence # noqa: PLC0415
        from ..models.run_switch_phase import RunSwitchPhase # noqa: PLC0415
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        from ..models.run_switch_reason import RunSwitchReason # noqa: PLC0415
        from ..models.run_switch_reconciliation_authority import RunSwitchReconciliationAuthority # noqa: PLC0415
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact # noqa: PLC0415
        from ..models.spark_fit import SparkFit # noqa: PLC0415
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        from ..models.stop_impact import StopImpact # noqa: PLC0415
        action: str = self.action

        alias: None | str
        alias = self.alias

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        build = self.build.to_dict()

        conflicts = []
        for conflicts_item_data in self.conflicts:
            conflicts_item = conflicts_item_data.to_dict()
            conflicts.append(conflicts_item)



        fit = self.fit.to_dict()

        fit_after_stop: dict[str, Any] | None
        if isinstance(self.fit_after_stop, SparkFit):
            fit_after_stop = self.fit_after_stop.to_dict()
        else:
            fit_after_stop = self.fit_after_stop

        fit_current = self.fit_current.to_dict()

        generated_at = self.generated_at.isoformat()

        image_digest: None | str
        image_digest = self.image_digest

        installation_id: None | str
        installation_id = self.installation_id

        installation_state: None | str
        installation_state = self.installation_state

        invocation = self.invocation.to_dict()

        mapping: dict[str, Any] | None
        if isinstance(self.mapping, MappingSelection):
            mapping = self.mapping.to_dict()
        else:
            mapping = self.mapping

        model_content_sha256: None | str
        model_content_sha256 = self.model_content_sha256

        phases = []
        for phases_item_data in self.phases:
            phases_item = phases_item_data.to_dict()
            phases.append(phases_item)



        plan_digest = self.plan_digest

        recipe_build_id: None | str
        recipe_build_id = self.recipe_build_id

        recipe_content_sha256: None | str
        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id: None | str
        recipe_revision_id = self.recipe_revision_id

        reclaimed_bytes = self.reclaimed_bytes

        run_id: None | str
        run_id = self.run_id

        runtime_storage = self.runtime_storage.to_dict()

        spark_group = self.spark_group.to_dict()

        start_plan_digest: None | str
        start_plan_digest = self.start_plan_digest

        stops = []
        for stops_item_data in self.stops:
            stops_item = stops_item_data.to_dict()
            stops.append(stops_item)



        storage = self.storage.to_dict()

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        cleanup_disposition: str | Unset = UNSET
        if not isinstance(self.cleanup_disposition, Unset):
            cleanup_disposition = self.cleanup_disposition


        cleanup_mode: str | Unset = UNSET
        if not isinstance(self.cleanup_mode, Unset):
            cleanup_mode = self.cleanup_mode


        effective_settings: dict[str, Any] | None | Unset
        if isinstance(self.effective_settings, Unset):
            effective_settings = UNSET
        elif isinstance(self.effective_settings, EffectiveSettingsSelection):
            effective_settings = self.effective_settings.to_dict()
        else:
            effective_settings = self.effective_settings

        freshness: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.freshness, Unset):
            freshness = []
            for freshness_item_data in self.freshness:
                freshness_item = freshness_item_data.to_dict()
                freshness.append(freshness_item)



        post_stop_memory_check: dict[str, Any] | None | Unset
        if isinstance(self.post_stop_memory_check, Unset):
            post_stop_memory_check = UNSET
        elif isinstance(self.post_stop_memory_check, ConditionalPostStopMemoryCheck):
            post_stop_memory_check = self.post_stop_memory_check.to_dict()
        else:
            post_stop_memory_check = self.post_stop_memory_check

        preparation: dict[str, Any] | None | Unset
        if isinstance(self.preparation, Unset):
            preparation = UNSET
        elif isinstance(self.preparation, RolloutPreparation):
            preparation = self.preparation.to_dict()
        else:
            preparation = self.preparation

        profile_stop_scope: dict[str, Any] | None | Unset
        if isinstance(self.profile_stop_scope, Unset):
            profile_stop_scope = UNSET
        elif isinstance(self.profile_stop_scope, RunSwitchProfileStopScope):
            profile_stop_scope = self.profile_stop_scope.to_dict()
        else:
            profile_stop_scope = self.profile_stop_scope

        reconciliation_authority: dict[str, Any] | None | Unset
        if isinstance(self.reconciliation_authority, Unset):
            reconciliation_authority = UNSET
        elif isinstance(self.reconciliation_authority, RunSwitchReconciliationAuthority):
            reconciliation_authority = self.reconciliation_authority.to_dict()
        else:
            reconciliation_authority = self.reconciliation_authority

        schema_version = self.schema_version

        stop_before_prepare = self.stop_before_prepare

        stop_before_transfer = self.stop_before_transfer


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "alias": alias,
            "allowed": allowed,
            "blockers": blockers,
            "build": build,
            "conflicts": conflicts,
            "fit": fit,
            "fit_after_stop": fit_after_stop,
            "fit_current": fit_current,
            "generated_at": generated_at,
            "image_digest": image_digest,
            "installation_id": installation_id,
            "installation_state": installation_state,
            "invocation": invocation,
            "mapping": mapping,
            "model_content_sha256": model_content_sha256,
            "phases": phases,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "reclaimed_bytes": reclaimed_bytes,
            "run_id": run_id,
            "runtime_storage": runtime_storage,
            "spark_group": spark_group,
            "start_plan_digest": start_plan_digest,
            "stops": stops,
            "storage": storage,
            "warnings": warnings,
        })
        if cleanup_disposition is not UNSET:
            field_dict["cleanup_disposition"] = cleanup_disposition
        if cleanup_mode is not UNSET:
            field_dict["cleanup_mode"] = cleanup_mode
        if effective_settings is not UNSET:
            field_dict["effective_settings"] = effective_settings
        if freshness is not UNSET:
            field_dict["freshness"] = freshness
        if post_stop_memory_check is not UNSET:
            field_dict["post_stop_memory_check"] = post_stop_memory_check
        if preparation is not UNSET:
            field_dict["preparation"] = preparation
        if profile_stop_scope is not UNSET:
            field_dict["profile_stop_scope"] = profile_stop_scope
        if reconciliation_authority is not UNSET:
            field_dict["reconciliation_authority"] = reconciliation_authority
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if stop_before_prepare is not UNSET:
            field_dict["stop_before_prepare"] = stop_before_prepare
        if stop_before_transfer is not UNSET:
            field_dict["stop_before_transfer"] = stop_before_transfer

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_storage_impact import ArtifactStorageImpact # noqa: PLC0415
        from ..models.conditional_post_stop_memory_check import ConditionalPostStopMemoryCheck # noqa: PLC0415
        from ..models.effective_settings_selection import EffectiveSettingsSelection # noqa: PLC0415
        from ..models.freshness_evidence import FreshnessEvidence # noqa: PLC0415
        from ..models.invocation_metadata import InvocationMetadata # noqa: PLC0415
        from ..models.mapping_selection import MappingSelection # noqa: PLC0415
        from ..models.rollout_preparation import RolloutPreparation # noqa: PLC0415
        from ..models.run_switch_build_evidence import RunSwitchBuildEvidence # noqa: PLC0415
        from ..models.run_switch_phase import RunSwitchPhase # noqa: PLC0415
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        from ..models.run_switch_reason import RunSwitchReason # noqa: PLC0415
        from ..models.run_switch_reconciliation_authority import RunSwitchReconciliationAuthority # noqa: PLC0415
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact # noqa: PLC0415
        from ..models.spark_fit import SparkFit # noqa: PLC0415
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        from ..models.stop_impact import StopImpact # noqa: PLC0415
        d = dict(src_dict)
        action = check_run_switch_plan_action(d.pop("action"))




        def _parse_alias(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        alias = _parse_alias(d.pop("alias"))


        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        build = RunSwitchBuildEvidence.from_dict(d.pop("build"))




        conflicts = []
        _conflicts = d.pop("conflicts")
        for conflicts_item_data in (_conflicts):
            conflicts_item = RunSwitchReason.from_dict(conflicts_item_data)



            conflicts.append(conflicts_item)


        fit = SparkFit.from_dict(d.pop("fit"))




        def _parse_fit_after_stop(data: object) -> None | SparkFit:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fit_after_stop_type_0 = SparkFit.from_dict(data)



                return fit_after_stop_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SparkFit, data)

        fit_after_stop = _parse_fit_after_stop(d.pop("fit_after_stop"))


        fit_current = SparkFit.from_dict(d.pop("fit_current"))




        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        def _parse_image_digest(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        def _parse_installation_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        installation_id = _parse_installation_id(d.pop("installation_id"))


        def _parse_installation_state(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        installation_state = _parse_installation_state(d.pop("installation_state"))


        invocation = InvocationMetadata.from_dict(d.pop("invocation"))




        def _parse_mapping(data: object) -> MappingSelection | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                mapping_type_0 = MappingSelection.from_dict(data)



                return mapping_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MappingSelection | None, data)

        mapping = _parse_mapping(d.pop("mapping"))


        def _parse_model_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256"))


        phases = []
        _phases = d.pop("phases")
        for phases_item_data in (_phases):
            phases_item = RunSwitchPhase.from_dict(phases_item_data)



            phases.append(phases_item)


        plan_digest = d.pop("plan_digest")

        def _parse_recipe_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        def _parse_recipe_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_content_sha256 = _parse_recipe_content_sha256(d.pop("recipe_content_sha256"))


        def _parse_recipe_revision_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id"))


        reclaimed_bytes = d.pop("reclaimed_bytes")

        def _parse_run_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        run_id = _parse_run_id(d.pop("run_id"))


        runtime_storage = RuntimeImageStorageImpact.from_dict(d.pop("runtime_storage"))




        spark_group = SparkGroup.from_dict(d.pop("spark_group"))




        def _parse_start_plan_digest(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        start_plan_digest = _parse_start_plan_digest(d.pop("start_plan_digest"))


        stops = []
        _stops = d.pop("stops")
        for stops_item_data in (_stops):
            stops_item = StopImpact.from_dict(stops_item_data)



            stops.append(stops_item)


        storage = ArtifactStorageImpact.from_dict(d.pop("storage"))




        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = RunSwitchReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        _cleanup_disposition = d.pop("cleanup_disposition", UNSET)
        cleanup_disposition: RunSwitchPlanCleanupDisposition | Unset
        if isinstance(_cleanup_disposition,  Unset):
            cleanup_disposition = UNSET
        else:
            cleanup_disposition = check_run_switch_plan_cleanup_disposition(_cleanup_disposition)




        _cleanup_mode = d.pop("cleanup_mode", UNSET)
        cleanup_mode: RunSwitchPlanCleanupMode | Unset
        if isinstance(_cleanup_mode,  Unset):
            cleanup_mode = UNSET
        else:
            cleanup_mode = check_run_switch_plan_cleanup_mode(_cleanup_mode)




        def _parse_effective_settings(data: object) -> EffectiveSettingsSelection | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_settings_type_0 = EffectiveSettingsSelection.from_dict(data)



                return effective_settings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EffectiveSettingsSelection | None | Unset, data)

        effective_settings = _parse_effective_settings(d.pop("effective_settings", UNSET))


        _freshness = d.pop("freshness", UNSET)
        freshness: list[FreshnessEvidence] | Unset = UNSET
        if _freshness is not UNSET:
            freshness = []
            for freshness_item_data in _freshness:
                freshness_item = FreshnessEvidence.from_dict(freshness_item_data)



                freshness.append(freshness_item)


        def _parse_post_stop_memory_check(data: object) -> ConditionalPostStopMemoryCheck | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                post_stop_memory_check_type_0 = ConditionalPostStopMemoryCheck.from_dict(data)



                return post_stop_memory_check_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ConditionalPostStopMemoryCheck | None | Unset, data)

        post_stop_memory_check = _parse_post_stop_memory_check(d.pop("post_stop_memory_check", UNSET))


        def _parse_preparation(data: object) -> None | RolloutPreparation | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preparation_type_0 = RolloutPreparation.from_dict(data)



                return preparation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RolloutPreparation | Unset, data)

        preparation = _parse_preparation(d.pop("preparation", UNSET))


        def _parse_profile_stop_scope(data: object) -> None | RunSwitchProfileStopScope | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_stop_scope_type_0 = RunSwitchProfileStopScope.from_dict(data)



                return profile_stop_scope_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchProfileStopScope | Unset, data)

        profile_stop_scope = _parse_profile_stop_scope(d.pop("profile_stop_scope", UNSET))


        def _parse_reconciliation_authority(data: object) -> None | RunSwitchReconciliationAuthority | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                reconciliation_authority_type_0 = RunSwitchReconciliationAuthority.from_dict(data)



                return reconciliation_authority_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchReconciliationAuthority | Unset, data)

        reconciliation_authority = _parse_reconciliation_authority(d.pop("reconciliation_authority", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        stop_before_prepare = d.pop("stop_before_prepare", UNSET)

        stop_before_transfer = d.pop("stop_before_transfer", UNSET)

        run_switch_plan = cls(
            action=action,
            alias=alias,
            allowed=allowed,
            blockers=blockers,
            build=build,
            conflicts=conflicts,
            fit=fit,
            fit_after_stop=fit_after_stop,
            fit_current=fit_current,
            generated_at=generated_at,
            image_digest=image_digest,
            installation_id=installation_id,
            installation_state=installation_state,
            invocation=invocation,
            mapping=mapping,
            model_content_sha256=model_content_sha256,
            phases=phases,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            reclaimed_bytes=reclaimed_bytes,
            run_id=run_id,
            runtime_storage=runtime_storage,
            spark_group=spark_group,
            start_plan_digest=start_plan_digest,
            stops=stops,
            storage=storage,
            warnings=warnings,
            cleanup_disposition=cleanup_disposition,
            cleanup_mode=cleanup_mode,
            effective_settings=effective_settings,
            freshness=freshness,
            post_stop_memory_check=post_stop_memory_check,
            preparation=preparation,
            profile_stop_scope=profile_stop_scope,
            reconciliation_authority=reconciliation_authority,
            schema_version=schema_version,
            stop_before_prepare=stop_before_prepare,
            stop_before_transfer=stop_before_transfer,
        )

        return run_switch_plan
