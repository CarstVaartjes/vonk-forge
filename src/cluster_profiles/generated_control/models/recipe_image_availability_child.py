from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_image_availability_child_kind import check_recipe_image_availability_child_kind
from ..models.recipe_image_availability_child_kind import RecipeImageAvailabilityChildKind
from ..models.recipe_image_availability_child_state import check_recipe_image_availability_child_state
from ..models.recipe_image_availability_child_state import RecipeImageAvailabilityChildState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="RecipeImageAvailabilityChild")



@_attrs_define
class RecipeImageAvailabilityChild:
    """
        Attributes:
            id (str):
            kind (RecipeImageAvailabilityChildKind):
            progress (OperationProgress): Canonical progress payload persisted on the current operation attempt.
            state (RecipeImageAvailabilityChildState):
            artifact_set_sha256 (Union[None, Unset, str]):
            artifacts (Union[Unset, list['RecipeImageAvailabilityArtifact']]):
            failure (Union['AvailabilityOperationFailure', None, Unset]):
            model_versions (Union[Unset, list[str]]):
            plan_digest (Union[None, Unset, str]):
            request_key (Union[None, Unset, str]):
     """

    id: str
    kind: RecipeImageAvailabilityChildKind
    progress: 'OperationProgress'
    state: RecipeImageAvailabilityChildState
    artifact_set_sha256: Union[None, Unset, str] = UNSET
    artifacts: Union[Unset, list['RecipeImageAvailabilityArtifact']] = UNSET
    failure: Union['AvailabilityOperationFailure', None, Unset] = UNSET
    model_versions: Union[Unset, list[str]] = UNSET
    plan_digest: Union[None, Unset, str] = UNSET
    request_key: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.operation_progress import OperationProgress
        id = self.id

        kind: str = self.kind

        progress = self.progress.to_dict()

        state: str = self.state

        artifact_set_sha256: Union[None, Unset, str]
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        artifacts: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)



        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        model_versions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model_versions, Unset):
            model_versions = self.model_versions



        plan_digest: Union[None, Unset, str]
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: Union[None, Unset, str]
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
            "progress": progress,
            "state": state,
        })
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if failure is not UNSET:
            field_dict["failure"] = failure
        if model_versions is not UNSET:
            field_dict["model_versions"] = model_versions
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.operation_progress import OperationProgress
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_recipe_image_availability_child_kind(d.pop("kind"))




        progress = OperationProgress.from_dict(d.pop("progress"))




        state = check_recipe_image_availability_child_state(d.pop("state"))




        def _parse_artifact_set_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        artifacts = []
        _artifacts = d.pop("artifacts", UNSET)
        for artifacts_item_data in (_artifacts or []):
            artifacts_item = RecipeImageAvailabilityArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        def _parse_failure(data: object) -> Union['AvailabilityOperationFailure', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            return cast(Union['AvailabilityOperationFailure', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        model_versions = cast(list[str], d.pop("model_versions", UNSET))


        def _parse_plan_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        recipe_image_availability_child = cls(
            id=id,
            kind=kind,
            progress=progress,
            state=state,
            artifact_set_sha256=artifact_set_sha256,
            artifacts=artifacts,
            failure=failure,
            model_versions=model_versions,
            plan_digest=plan_digest,
            request_key=request_key,
        )

        return recipe_image_availability_child
