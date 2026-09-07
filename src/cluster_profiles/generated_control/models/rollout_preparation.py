from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.runtime_image_preparation import RuntimeImagePreparation
  from ..models.model_artifact_preparation import ModelArtifactPreparation
  from ..models.compatibility_preparation import CompatibilityPreparation
  from ..models.preparation_reason import PreparationReason





T = TypeVar("T", bound="RolloutPreparation")



@_attrs_define
class RolloutPreparation:
    """ Normalized preparation identity shared by profiles, Run, web and CLI.

        Attributes:
            controller_ready (bool):
            model (ModelArtifactPreparation): Complete exact model set, including auxiliary and dependency files.
            ready (bool):
            runtime_image (RuntimeImagePreparation): Exact executable OCI image kept separate from model payloads.
            target_node_ids (list[str]):
            targets_ready (bool):
            exceptions (Union[Unset, list['CompatibilityPreparation']]):
            reasons (Union[Unset, list['PreparationReason']]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    controller_ready: bool
    model: 'ModelArtifactPreparation'
    ready: bool
    runtime_image: 'RuntimeImagePreparation'
    target_node_ids: list[str]
    targets_ready: bool
    exceptions: Union[Unset, list['CompatibilityPreparation']] = UNSET
    reasons: Union[Unset, list['PreparationReason']] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.runtime_image_preparation import RuntimeImagePreparation
        from ..models.model_artifact_preparation import ModelArtifactPreparation
        from ..models.compatibility_preparation import CompatibilityPreparation
        from ..models.preparation_reason import PreparationReason
        controller_ready = self.controller_ready

        model = self.model.to_dict()

        ready = self.ready

        runtime_image = self.runtime_image.to_dict()

        target_node_ids = self.target_node_ids



        targets_ready = self.targets_ready

        exceptions: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.exceptions, Unset):
            exceptions = []
            for exceptions_item_data in self.exceptions:
                exceptions_item = exceptions_item_data.to_dict()
                exceptions.append(exceptions_item)



        reasons: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "controller_ready": controller_ready,
            "model": model,
            "ready": ready,
            "runtime_image": runtime_image,
            "target_node_ids": target_node_ids,
            "targets_ready": targets_ready,
        })
        if exceptions is not UNSET:
            field_dict["exceptions"] = exceptions
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.runtime_image_preparation import RuntimeImagePreparation
        from ..models.model_artifact_preparation import ModelArtifactPreparation
        from ..models.compatibility_preparation import CompatibilityPreparation
        from ..models.preparation_reason import PreparationReason
        d = dict(src_dict)
        controller_ready = d.pop("controller_ready")

        model = ModelArtifactPreparation.from_dict(d.pop("model"))




        ready = d.pop("ready")

        runtime_image = RuntimeImagePreparation.from_dict(d.pop("runtime_image"))




        target_node_ids = cast(list[str], d.pop("target_node_ids"))


        targets_ready = d.pop("targets_ready")

        exceptions = []
        _exceptions = d.pop("exceptions", UNSET)
        for exceptions_item_data in (_exceptions or []):
            exceptions_item = CompatibilityPreparation.from_dict(exceptions_item_data)



            exceptions.append(exceptions_item)


        reasons = []
        _reasons = d.pop("reasons", UNSET)
        for reasons_item_data in (_reasons or []):
            reasons_item = PreparationReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        rollout_preparation = cls(
            controller_ready=controller_ready,
            model=model,
            ready=ready,
            runtime_image=runtime_image,
            target_node_ids=target_node_ids,
            targets_ready=targets_ready,
            exceptions=exceptions,
            reasons=reasons,
            schema_version=schema_version,
        )

        return rollout_preparation
