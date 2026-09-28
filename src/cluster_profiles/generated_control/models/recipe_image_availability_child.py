from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_image_availability_child_kind import check_recipe_image_availability_child_kind
from ..models.recipe_image_availability_child_kind import RecipeImageAvailabilityChildKind
from ..models.recipe_image_availability_child_state import check_recipe_image_availability_child_state
from ..models.recipe_image_availability_child_state import RecipeImageAvailabilityChildState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.operation_progress import OperationProgress
  from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact





T = TypeVar("T", bound="RecipeImageAvailabilityChild")



@_attrs_define
class RecipeImageAvailabilityChild:
    """
        Attributes:
            id (str):
            kind (RecipeImageAvailabilityChildKind):
            model_content_digests (list[str]):
            progress (OperationProgress): Canonical durable progress payload shared by Controller and agents.
            state (RecipeImageAvailabilityChildState):
            artifact_set_sha256 (None | str | Unset):
            artifacts (list[RecipeImageAvailabilityArtifact] | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            plan_digest (None | str | Unset):
            request_key (None | str | Unset):
     """

    id: str
    kind: RecipeImageAvailabilityChildKind
    model_content_digests: list[str]
    progress: OperationProgress
    state: RecipeImageAvailabilityChildState
    artifact_set_sha256: None | str | Unset = UNSET
    artifacts: list[RecipeImageAvailabilityArtifact] | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    request_key: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact # noqa: PLC0415
        id = self.id

        kind: str = self.kind

        model_content_digests = self.model_content_digests



        progress = self.progress.to_dict()

        state: str = self.state

        artifact_set_sha256: None | str | Unset
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        artifacts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)



        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
            "model_content_digests": model_content_digests,
            "progress": progress,
            "state": state,
        })
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if failure is not UNSET:
            field_dict["failure"] = failure
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_recipe_image_availability_child_kind(d.pop("kind"))




        model_content_digests = cast(list[str], d.pop("model_content_digests"))


        progress = OperationProgress.from_dict(d.pop("progress"))




        state = check_recipe_image_availability_child_state(d.pop("state"))




        def _parse_artifact_set_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        _artifacts = d.pop("artifacts", UNSET)
        artifacts: list[RecipeImageAvailabilityArtifact] | Unset = UNSET
        if _artifacts is not UNSET:
            artifacts = []
            for artifacts_item_data in _artifacts:
                artifacts_item = RecipeImageAvailabilityArtifact.from_dict(artifacts_item_data)



                artifacts.append(artifacts_item)


        def _parse_failure(data: object) -> AvailabilityOperationFailure | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityOperationFailure | None | Unset, data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        recipe_image_availability_child = cls(
            id=id,
            kind=kind,
            model_content_digests=model_content_digests,
            progress=progress,
            state=state,
            artifact_set_sha256=artifact_set_sha256,
            artifacts=artifacts,
            failure=failure,
            plan_digest=plan_digest,
            request_key=request_key,
        )

        return recipe_image_availability_child
