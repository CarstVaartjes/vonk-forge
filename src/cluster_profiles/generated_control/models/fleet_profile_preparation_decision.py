from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_compatibility_decision import FleetProfileCompatibilityDecision
  from ..models.model_artifact_identity import ModelArtifactIdentity
  from ..models.runtime_image_identity import RuntimeImageIdentity
  from ..models.preparation_reason import PreparationReason





T = TypeVar("T", bound="FleetProfilePreparationDecision")



@_attrs_define
class FleetProfilePreparationDecision:
    """ Exact assets and reuse decisions; byte counters are observations only.

        Attributes:
            assignment_id (str):
            blockers (list['PreparationReason']):
            exceptions (list['FleetProfileCompatibilityDecision']):
            image_controller_ready (bool):
            image_reuse_node_ids (list[str]):
            model (ModelArtifactIdentity): Exact model set, independent of transfer progress and verification time.
            model_complete (bool):
            model_controller_ready (bool):
            model_reuse_node_ids (list[str]):
            runtime_image (RuntimeImageIdentity): Executable OCI identity, independent of its transfer observations.
     """

    assignment_id: str
    blockers: list['PreparationReason']
    exceptions: list['FleetProfileCompatibilityDecision']
    image_controller_ready: bool
    image_reuse_node_ids: list[str]
    model: 'ModelArtifactIdentity'
    model_complete: bool
    model_controller_ready: bool
    model_reuse_node_ids: list[str]
    runtime_image: 'RuntimeImageIdentity'





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_compatibility_decision import FleetProfileCompatibilityDecision
        from ..models.model_artifact_identity import ModelArtifactIdentity
        from ..models.runtime_image_identity import RuntimeImageIdentity
        from ..models.preparation_reason import PreparationReason
        assignment_id = self.assignment_id

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        exceptions = []
        for exceptions_item_data in self.exceptions:
            exceptions_item = exceptions_item_data.to_dict()
            exceptions.append(exceptions_item)



        image_controller_ready = self.image_controller_ready

        image_reuse_node_ids = self.image_reuse_node_ids



        model = self.model.to_dict()

        model_complete = self.model_complete

        model_controller_ready = self.model_controller_ready

        model_reuse_node_ids = self.model_reuse_node_ids



        runtime_image = self.runtime_image.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignment_id": assignment_id,
            "blockers": blockers,
            "exceptions": exceptions,
            "image_controller_ready": image_controller_ready,
            "image_reuse_node_ids": image_reuse_node_ids,
            "model": model,
            "model_complete": model_complete,
            "model_controller_ready": model_controller_ready,
            "model_reuse_node_ids": model_reuse_node_ids,
            "runtime_image": runtime_image,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_compatibility_decision import FleetProfileCompatibilityDecision
        from ..models.model_artifact_identity import ModelArtifactIdentity
        from ..models.runtime_image_identity import RuntimeImageIdentity
        from ..models.preparation_reason import PreparationReason
        d = dict(src_dict)
        assignment_id = d.pop("assignment_id")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = PreparationReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        exceptions = []
        _exceptions = d.pop("exceptions")
        for exceptions_item_data in (_exceptions):
            exceptions_item = FleetProfileCompatibilityDecision.from_dict(exceptions_item_data)



            exceptions.append(exceptions_item)


        image_controller_ready = d.pop("image_controller_ready")

        image_reuse_node_ids = cast(list[str], d.pop("image_reuse_node_ids"))


        model = ModelArtifactIdentity.from_dict(d.pop("model"))




        model_complete = d.pop("model_complete")

        model_controller_ready = d.pop("model_controller_ready")

        model_reuse_node_ids = cast(list[str], d.pop("model_reuse_node_ids"))


        runtime_image = RuntimeImageIdentity.from_dict(d.pop("runtime_image"))




        fleet_profile_preparation_decision = cls(
            assignment_id=assignment_id,
            blockers=blockers,
            exceptions=exceptions,
            image_controller_ready=image_controller_ready,
            image_reuse_node_ids=image_reuse_node_ids,
            model=model,
            model_complete=model_complete,
            model_controller_ready=model_controller_ready,
            model_reuse_node_ids=model_reuse_node_ids,
            runtime_image=runtime_image,
        )

        return fleet_profile_preparation_decision
