from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_plan_action import check_run_switch_plan_action
from ..models.run_switch_plan_action import RunSwitchPlanAction
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.run_switch_phase import RunSwitchPhase
  from ..models.mapping_selection import MappingSelection
  from ..models.spark_group import SparkGroup
  from ..models.artifact_storage_impact import ArtifactStorageImpact
  from ..models.capability_evidence import CapabilityEvidence
  from ..models.effective_settings_selection import EffectiveSettingsSelection
  from ..models.run_switch_reason import RunSwitchReason
  from ..models.recipe_build_evidence import RecipeBuildEvidence
  from ..models.rollout_preparation import RolloutPreparation
  from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
  from ..models.invocation_metadata import InvocationMetadata
  from ..models.spark_fit import SparkFit
  from ..models.freshness_evidence import FreshnessEvidence
  from ..models.stop_impact import StopImpact





T = TypeVar("T", bound="RunSwitchPlan")



@_attrs_define
class RunSwitchPlan:
    """
        Attributes:
            action (RunSwitchPlanAction):
            alias (Union[None, str]):
            allowed (bool):
            blockers (list['RunSwitchReason']):
            build (RecipeBuildEvidence):
            conflicts (list['RunSwitchReason']):
            fit (SparkFit):
            fit_after_stop (Union['SparkFit', None]):
            fit_current (SparkFit):
            freshness (list['FreshnessEvidence']):
            generated_at (datetime.datetime):
            image_digest (Union[None, str]):
            installation_id (Union[None, str]):
            installation_state (Union[None, str]):
            invocation (InvocationMetadata): Context for audit and tracing which has no decision-making authority.
            mapping (Union['MappingSelection', None]):
            model_capabilities (list['CapabilityEvidence']):
            model_version_sha256 (Union[None, str]):
            phases (list['RunSwitchPhase']):
            plan_digest (str):
            recipe_build_id (Union[None, str]):
            recipe_capabilities (list['CapabilityEvidence']):
            recipe_content_sha256 (Union[None, str]):
            recipe_revision_id (Union[None, str]):
            reclaimed_bytes (int):
            run_id (Union[None, str]):
            runtime_storage (RuntimeImageStorageImpact):
            spark_group (SparkGroup): A complete, rank-labelled Spark group selected by the operator.
            start_plan_digest (Union[None, str]):
            stops (list['StopImpact']):
            storage (ArtifactStorageImpact): Byte impact with unknown values preserved as unknown, never guessed.
            warnings (list['RunSwitchReason']):
            effective_settings (Union['EffectiveSettingsSelection', None, Unset]):
            preparation (Union['RolloutPreparation', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            stop_before_prepare (Union[Unset, bool]):  Default: False.
            stop_before_transfer (Union[Unset, bool]):  Default: False.
     """

    action: RunSwitchPlanAction
    alias: Union[None, str]
    allowed: bool
    blockers: list['RunSwitchReason']
    build: 'RecipeBuildEvidence'
    conflicts: list['RunSwitchReason']
    fit: 'SparkFit'
    fit_after_stop: Union['SparkFit', None]
    fit_current: 'SparkFit'
    freshness: list['FreshnessEvidence']
    generated_at: datetime.datetime
    image_digest: Union[None, str]
    installation_id: Union[None, str]
    installation_state: Union[None, str]
    invocation: 'InvocationMetadata'
    mapping: Union['MappingSelection', None]
    model_capabilities: list['CapabilityEvidence']
    model_version_sha256: Union[None, str]
    phases: list['RunSwitchPhase']
    plan_digest: str
    recipe_build_id: Union[None, str]
    recipe_capabilities: list['CapabilityEvidence']
    recipe_content_sha256: Union[None, str]
    recipe_revision_id: Union[None, str]
    reclaimed_bytes: int
    run_id: Union[None, str]
    runtime_storage: 'RuntimeImageStorageImpact'
    spark_group: 'SparkGroup'
    start_plan_digest: Union[None, str]
    stops: list['StopImpact']
    storage: 'ArtifactStorageImpact'
    warnings: list['RunSwitchReason']
    effective_settings: Union['EffectiveSettingsSelection', None, Unset] = UNSET
    preparation: Union['RolloutPreparation', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    stop_before_prepare: Union[Unset, bool] = False
    stop_before_transfer: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_phase import RunSwitchPhase
        from ..models.mapping_selection import MappingSelection
        from ..models.spark_group import SparkGroup
        from ..models.artifact_storage_impact import ArtifactStorageImpact
        from ..models.capability_evidence import CapabilityEvidence
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.recipe_build_evidence import RecipeBuildEvidence
        from ..models.rollout_preparation import RolloutPreparation
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
        from ..models.invocation_metadata import InvocationMetadata
        from ..models.spark_fit import SparkFit
        from ..models.freshness_evidence import FreshnessEvidence
        from ..models.stop_impact import StopImpact
        action: str = self.action

        alias: Union[None, str]
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

        fit_after_stop: Union[None, dict[str, Any]]
        if isinstance(self.fit_after_stop, SparkFit):
            fit_after_stop = self.fit_after_stop.to_dict()
        else:
            fit_after_stop = self.fit_after_stop

        fit_current = self.fit_current.to_dict()

        freshness = []
        for freshness_item_data in self.freshness:
            freshness_item = freshness_item_data.to_dict()
            freshness.append(freshness_item)



        generated_at = self.generated_at.isoformat()

        image_digest: Union[None, str]
        image_digest = self.image_digest

        installation_id: Union[None, str]
        installation_id = self.installation_id

        installation_state: Union[None, str]
        installation_state = self.installation_state

        invocation = self.invocation.to_dict()

        mapping: Union[None, dict[str, Any]]
        if isinstance(self.mapping, MappingSelection):
            mapping = self.mapping.to_dict()
        else:
            mapping = self.mapping

        model_capabilities = []
        for model_capabilities_item_data in self.model_capabilities:
            model_capabilities_item = model_capabilities_item_data.to_dict()
            model_capabilities.append(model_capabilities_item)



        model_version_sha256: Union[None, str]
        model_version_sha256 = self.model_version_sha256

        phases = []
        for phases_item_data in self.phases:
            phases_item = phases_item_data.to_dict()
            phases.append(phases_item)



        plan_digest = self.plan_digest

        recipe_build_id: Union[None, str]
        recipe_build_id = self.recipe_build_id

        recipe_capabilities = []
        for recipe_capabilities_item_data in self.recipe_capabilities:
            recipe_capabilities_item = recipe_capabilities_item_data.to_dict()
            recipe_capabilities.append(recipe_capabilities_item)



        recipe_content_sha256: Union[None, str]
        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id: Union[None, str]
        recipe_revision_id = self.recipe_revision_id

        reclaimed_bytes = self.reclaimed_bytes

        run_id: Union[None, str]
        run_id = self.run_id

        runtime_storage = self.runtime_storage.to_dict()

        spark_group = self.spark_group.to_dict()

        start_plan_digest: Union[None, str]
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



        effective_settings: Union[None, Unset, dict[str, Any]]
        if isinstance(self.effective_settings, Unset):
            effective_settings = UNSET
        elif isinstance(self.effective_settings, EffectiveSettingsSelection):
            effective_settings = self.effective_settings.to_dict()
        else:
            effective_settings = self.effective_settings

        preparation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.preparation, Unset):
            preparation = UNSET
        elif isinstance(self.preparation, RolloutPreparation):
            preparation = self.preparation.to_dict()
        else:
            preparation = self.preparation

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
            "freshness": freshness,
            "generated_at": generated_at,
            "image_digest": image_digest,
            "installation_id": installation_id,
            "installation_state": installation_state,
            "invocation": invocation,
            "mapping": mapping,
            "model_capabilities": model_capabilities,
            "model_version_sha256": model_version_sha256,
            "phases": phases,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_capabilities": recipe_capabilities,
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
        if effective_settings is not UNSET:
            field_dict["effective_settings"] = effective_settings
        if preparation is not UNSET:
            field_dict["preparation"] = preparation
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if stop_before_prepare is not UNSET:
            field_dict["stop_before_prepare"] = stop_before_prepare
        if stop_before_transfer is not UNSET:
            field_dict["stop_before_transfer"] = stop_before_transfer

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_phase import RunSwitchPhase
        from ..models.mapping_selection import MappingSelection
        from ..models.spark_group import SparkGroup
        from ..models.artifact_storage_impact import ArtifactStorageImpact
        from ..models.capability_evidence import CapabilityEvidence
        from ..models.effective_settings_selection import EffectiveSettingsSelection
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.recipe_build_evidence import RecipeBuildEvidence
        from ..models.rollout_preparation import RolloutPreparation
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
        from ..models.invocation_metadata import InvocationMetadata
        from ..models.spark_fit import SparkFit
        from ..models.freshness_evidence import FreshnessEvidence
        from ..models.stop_impact import StopImpact
        d = dict(src_dict)
        action = check_run_switch_plan_action(d.pop("action"))




        def _parse_alias(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        alias = _parse_alias(d.pop("alias"))


        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        build = RecipeBuildEvidence.from_dict(d.pop("build"))




        conflicts = []
        _conflicts = d.pop("conflicts")
        for conflicts_item_data in (_conflicts):
            conflicts_item = RunSwitchReason.from_dict(conflicts_item_data)



            conflicts.append(conflicts_item)


        fit = SparkFit.from_dict(d.pop("fit"))




        def _parse_fit_after_stop(data: object) -> Union['SparkFit', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fit_after_stop_type_0 = SparkFit.from_dict(data)



                return fit_after_stop_type_0
            except: # noqa: E722
                pass
            return cast(Union['SparkFit', None], data)

        fit_after_stop = _parse_fit_after_stop(d.pop("fit_after_stop"))


        fit_current = SparkFit.from_dict(d.pop("fit_current"))




        freshness = []
        _freshness = d.pop("freshness")
        for freshness_item_data in (_freshness):
            freshness_item = FreshnessEvidence.from_dict(freshness_item_data)



            freshness.append(freshness_item)


        generated_at = isoparse(d.pop("generated_at"))




        def _parse_image_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        def _parse_installation_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        installation_id = _parse_installation_id(d.pop("installation_id"))


        def _parse_installation_state(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        installation_state = _parse_installation_state(d.pop("installation_state"))


        invocation = InvocationMetadata.from_dict(d.pop("invocation"))




        def _parse_mapping(data: object) -> Union['MappingSelection', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                mapping_type_0 = MappingSelection.from_dict(data)



                return mapping_type_0
            except: # noqa: E722
                pass
            return cast(Union['MappingSelection', None], data)

        mapping = _parse_mapping(d.pop("mapping"))


        model_capabilities = []
        _model_capabilities = d.pop("model_capabilities")
        for model_capabilities_item_data in (_model_capabilities):
            model_capabilities_item = CapabilityEvidence.from_dict(model_capabilities_item_data)



            model_capabilities.append(model_capabilities_item)


        def _parse_model_version_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        model_version_sha256 = _parse_model_version_sha256(d.pop("model_version_sha256"))


        phases = []
        _phases = d.pop("phases")
        for phases_item_data in (_phases):
            phases_item = RunSwitchPhase.from_dict(phases_item_data)



            phases.append(phases_item)


        plan_digest = d.pop("plan_digest")

        def _parse_recipe_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        recipe_capabilities = []
        _recipe_capabilities = d.pop("recipe_capabilities")
        for recipe_capabilities_item_data in (_recipe_capabilities):
            recipe_capabilities_item = CapabilityEvidence.from_dict(recipe_capabilities_item_data)



            recipe_capabilities.append(recipe_capabilities_item)


        def _parse_recipe_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_content_sha256 = _parse_recipe_content_sha256(d.pop("recipe_content_sha256"))


        def _parse_recipe_revision_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id"))


        reclaimed_bytes = d.pop("reclaimed_bytes")

        def _parse_run_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        run_id = _parse_run_id(d.pop("run_id"))


        runtime_storage = RuntimeImageStorageImpact.from_dict(d.pop("runtime_storage"))




        spark_group = SparkGroup.from_dict(d.pop("spark_group"))




        def _parse_start_plan_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

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


        def _parse_effective_settings(data: object) -> Union['EffectiveSettingsSelection', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_settings_type_0 = EffectiveSettingsSelection.from_dict(data)



                return effective_settings_type_0
            except: # noqa: E722
                pass
            return cast(Union['EffectiveSettingsSelection', None, Unset], data)

        effective_settings = _parse_effective_settings(d.pop("effective_settings", UNSET))


        def _parse_preparation(data: object) -> Union['RolloutPreparation', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preparation_type_0 = RolloutPreparation.from_dict(data)



                return preparation_type_0
            except: # noqa: E722
                pass
            return cast(Union['RolloutPreparation', None, Unset], data)

        preparation = _parse_preparation(d.pop("preparation", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
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
            freshness=freshness,
            generated_at=generated_at,
            image_digest=image_digest,
            installation_id=installation_id,
            installation_state=installation_state,
            invocation=invocation,
            mapping=mapping,
            model_capabilities=model_capabilities,
            model_version_sha256=model_version_sha256,
            phases=phases,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_capabilities=recipe_capabilities,
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
            effective_settings=effective_settings,
            preparation=preparation,
            schema_version=schema_version,
            stop_before_prepare=stop_before_prepare,
            stop_before_transfer=stop_before_transfer,
        )

        return run_switch_plan
