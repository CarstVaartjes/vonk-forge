from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.availability_model_child_state import AvailabilityModelChildState
from ..models.availability_model_child_state import check_availability_model_child_state
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.operation_progress import OperationProgress
  from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact





T = TypeVar("T", bound="AvailabilityModelChild")



@_attrs_define
class AvailabilityModelChild:
    """ The model-cache child an availability operation waits on, as last seen.

        Attributes:
            id (str):
            state (AvailabilityModelChildState):
            artifact_set_sha256 (None | str | Unset):
            artifacts (list[RecipeImageAvailabilityArtifact] | None | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            model_content_digests (list[str] | None | Unset):
            plan_digest (None | str | Unset):
            progress (None | OperationProgress | Unset):
            request_key (None | str | Unset):
     """

    id: str
    state: AvailabilityModelChildState
    artifact_set_sha256: None | str | Unset = UNSET
    artifacts: list[RecipeImageAvailabilityArtifact] | None | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    model_content_digests: list[str] | None | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    progress: None | OperationProgress | Unset = UNSET
    request_key: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_image_availability_artifact import RecipeImageAvailabilityArtifact # noqa: PLC0415
        id = self.id

        state: str = self.state

        artifact_set_sha256: None | str | Unset
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        artifacts: list[dict[str, Any]] | None | Unset
        if isinstance(self.artifacts, Unset):
            artifacts = UNSET
        elif isinstance(self.artifacts, list):
            artifacts = []
            for artifacts_type_0_item_data in self.artifacts:
                artifacts_type_0_item = artifacts_type_0_item_data.to_dict()
                artifacts.append(artifacts_type_0_item)


        else:
            artifacts = self.artifacts

        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        model_content_digests: list[str] | None | Unset
        if isinstance(self.model_content_digests, Unset):
            model_content_digests = UNSET
        elif isinstance(self.model_content_digests, list):
            model_content_digests = self.model_content_digests


        else:
            model_content_digests = self.model_content_digests

        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        progress: dict[str, Any] | None | Unset
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, OperationProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "state": state,
        })
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if failure is not UNSET:
            field_dict["failure"] = failure
        if model_content_digests is not UNSET:
            field_dict["model_content_digests"] = model_content_digests
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if progress is not UNSET:
            field_dict["progress"] = progress
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

        state = check_availability_model_child_state(d.pop("state"))




        def _parse_artifact_set_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        def _parse_artifacts(data: object) -> list[RecipeImageAvailabilityArtifact] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                artifacts_type_0 = []
                _artifacts_type_0 = data
                for artifacts_type_0_item_data in (_artifacts_type_0):
                    artifacts_type_0_item = RecipeImageAvailabilityArtifact.from_dict(artifacts_type_0_item_data)



                    artifacts_type_0.append(artifacts_type_0_item)

                return artifacts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[RecipeImageAvailabilityArtifact] | None | Unset, data)

        artifacts = _parse_artifacts(d.pop("artifacts", UNSET))


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


        def _parse_model_content_digests(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                model_content_digests_type_0 = cast(list[str], data)

                return model_content_digests_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        model_content_digests = _parse_model_content_digests(d.pop("model_content_digests", UNSET))


        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_progress(data: object) -> None | OperationProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = OperationProgress.from_dict(data)



                return progress_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationProgress | Unset, data)

        progress = _parse_progress(d.pop("progress", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        availability_model_child = cls(
            id=id,
            state=state,
            artifact_set_sha256=artifact_set_sha256,
            artifacts=artifacts,
            failure=failure,
            model_content_digests=model_content_digests,
            plan_digest=plan_digest,
            progress=progress,
            request_key=request_key,
        )

        return availability_model_child
