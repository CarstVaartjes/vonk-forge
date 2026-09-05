from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_artifact_preparation_completeness import check_model_artifact_preparation_completeness
from ..models.model_artifact_preparation_completeness import ModelArtifactPreparationCompleteness
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.controller_asset_state import ControllerAssetState
  from ..models.target_asset_state import TargetAssetState





T = TypeVar("T", bound="ModelArtifactPreparation")



@_attrs_define
class ModelArtifactPreparation:
    """ Complete exact model set, including auxiliary and dependency files.

        Attributes:
            artifact_count (int):
            artifact_set_bytes (int):
            artifact_set_sha256 (str):
            completeness (ModelArtifactPreparationCompleteness):
            controller (ControllerAssetState): Availability of one immutable asset in Controller/NAS storage.
            model_version_sha256 (str):
            targets (list['TargetAssetState']):
            dependency_model_version_sha256 (Union[Unset, list[str]]):
            recipe_revision_sha256 (Union[None, Unset, str]):
     """

    artifact_count: int
    artifact_set_bytes: int
    artifact_set_sha256: str
    completeness: ModelArtifactPreparationCompleteness
    controller: 'ControllerAssetState'
    model_version_sha256: str
    targets: list['TargetAssetState']
    dependency_model_version_sha256: Union[Unset, list[str]] = UNSET
    recipe_revision_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.controller_asset_state import ControllerAssetState
        from ..models.target_asset_state import TargetAssetState
        artifact_count = self.artifact_count

        artifact_set_bytes = self.artifact_set_bytes

        artifact_set_sha256 = self.artifact_set_sha256

        completeness: str = self.completeness

        controller = self.controller.to_dict()

        model_version_sha256 = self.model_version_sha256

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)



        dependency_model_version_sha256: Union[Unset, list[str]] = UNSET
        if not isinstance(self.dependency_model_version_sha256, Unset):
            dependency_model_version_sha256 = self.dependency_model_version_sha256



        recipe_revision_sha256: Union[None, Unset, str]
        if isinstance(self.recipe_revision_sha256, Unset):
            recipe_revision_sha256 = UNSET
        else:
            recipe_revision_sha256 = self.recipe_revision_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "artifact_set_bytes": artifact_set_bytes,
            "artifact_set_sha256": artifact_set_sha256,
            "completeness": completeness,
            "controller": controller,
            "model_version_sha256": model_version_sha256,
            "targets": targets,
        })
        if dependency_model_version_sha256 is not UNSET:
            field_dict["dependency_model_version_sha256"] = dependency_model_version_sha256
        if recipe_revision_sha256 is not UNSET:
            field_dict["recipe_revision_sha256"] = recipe_revision_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.controller_asset_state import ControllerAssetState
        from ..models.target_asset_state import TargetAssetState
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        artifact_set_bytes = d.pop("artifact_set_bytes")

        artifact_set_sha256 = d.pop("artifact_set_sha256")

        completeness = check_model_artifact_preparation_completeness(d.pop("completeness"))




        controller = ControllerAssetState.from_dict(d.pop("controller"))




        model_version_sha256 = d.pop("model_version_sha256")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in (_targets):
            targets_item = TargetAssetState.from_dict(targets_item_data)



            targets.append(targets_item)


        dependency_model_version_sha256 = cast(list[str], d.pop("dependency_model_version_sha256", UNSET))


        def _parse_recipe_revision_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256", UNSET))


        model_artifact_preparation = cls(
            artifact_count=artifact_count,
            artifact_set_bytes=artifact_set_bytes,
            artifact_set_sha256=artifact_set_sha256,
            completeness=completeness,
            controller=controller,
            model_version_sha256=model_version_sha256,
            targets=targets,
            dependency_model_version_sha256=dependency_model_version_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
        )

        return model_artifact_preparation
