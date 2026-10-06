from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.availability_model_child import AvailabilityModelChild
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.availability_retry import AvailabilityRetry
  from ..models.availability_runtime import AvailabilityRuntime
  from ..models.availability_supersession import AvailabilitySupersession
  from ..models.operation_blocker import OperationBlocker
  from ..models.operation_progress import OperationProgress
  from ..models.recipe_build_dependency import RecipeBuildDependency
  from ..models.recipe_definition import RecipeDefinition
  from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
  from ..models.recipe_retry_intent import RecipeRetryIntent
  from ..models.recipe_revision_intent import RecipeRevisionIntent
  from ..models.recipe_selector_intent import RecipeSelectorIntent
  from ..models.runtime_image_receipt import RuntimeImageReceipt
  from ..models.runtime_image_reference_intent import RuntimeImageReferenceIntent





T = TypeVar("T", bound="AvailabilityJobPayload")



@_attrs_define
class AvailabilityJobPayload:
    """ One image-availability operation: intent, identity, progress and what it holds.

        Attributes:
            force_rebuild (bool):
            kind (Literal['recipe.image.availability.v2']):
            progress (OperationProgress): Canonical durable progress payload shared by Controller and agents.
            recipe (RecipeDefinition): The sole public recipe authoring contract.
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            request (RecipeRetryIntent | RecipeRevisionIntent | RecipeSelectorIntent):
            retry (AvailabilityRetry):
            runtime (AvailabilityRuntime): The runtime projection an availability operation prepares an image for.

                The compiled adapter fields appear once the runtime is resolved; an
                operation that only reuses a cached image carries the identity subset.
            schema_version (Literal[2]):
            blockers (list[OperationBlocker] | None | Unset):
            build_dependency (None | RecipeBuildDependency | Unset):
            build_input_sha256 (None | str | Unset):
            cancellation (None | RecipeOperationCancellationResult | Unset):
            claim_owner (None | str | Unset):
            claim_until (datetime.datetime | None | Unset):
            effective_execution_key (None | str | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            identity_key (None | str | Unset):
            image_reference_intent (None | RuntimeImageReferenceIntent | Unset):
            image_result (None | RuntimeImageReceipt | Unset):
            model_child (AvailabilityModelChild | None | Unset):
            model_digest (None | str | Unset):
            prebuilt_pull (bool | None | Unset):
            removal_fence (None | str | Unset):
            retry_after_at (datetime.datetime | None | Unset):
            stage (Literal['available'] | None | Unset):
            supersession (AvailabilitySupersession | None | Unset):
     """

    force_rebuild: bool
    kind: Literal['recipe.image.availability.v2']
    progress: OperationProgress
    recipe: RecipeDefinition
    recipe_content_sha256: str
    recipe_revision_id: str
    request: RecipeRetryIntent | RecipeRevisionIntent | RecipeSelectorIntent
    retry: AvailabilityRetry
    runtime: AvailabilityRuntime
    schema_version: Literal[2]
    blockers: list[OperationBlocker] | None | Unset = UNSET
    build_dependency: None | RecipeBuildDependency | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    cancellation: None | RecipeOperationCancellationResult | Unset = UNSET
    claim_owner: None | str | Unset = UNSET
    claim_until: datetime.datetime | None | Unset = UNSET
    effective_execution_key: None | str | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    identity_key: None | str | Unset = UNSET
    image_reference_intent: None | RuntimeImageReferenceIntent | Unset = UNSET
    image_result: None | RuntimeImageReceipt | Unset = UNSET
    model_child: AvailabilityModelChild | None | Unset = UNSET
    model_digest: None | str | Unset = UNSET
    prebuilt_pull: bool | None | Unset = UNSET
    removal_fence: None | str | Unset = UNSET
    retry_after_at: datetime.datetime | None | Unset = UNSET
    stage: Literal['available'] | None | Unset = UNSET
    supersession: AvailabilitySupersession | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_model_child import AvailabilityModelChild # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.availability_retry import AvailabilityRetry # noqa: PLC0415
        from ..models.availability_runtime import AvailabilityRuntime # noqa: PLC0415
        from ..models.availability_supersession import AvailabilitySupersession # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_build_dependency import RecipeBuildDependency # noqa: PLC0415
        from ..models.recipe_definition import RecipeDefinition # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_retry_intent import RecipeRetryIntent # noqa: PLC0415
        from ..models.recipe_revision_intent import RecipeRevisionIntent # noqa: PLC0415
        from ..models.recipe_selector_intent import RecipeSelectorIntent # noqa: PLC0415
        from ..models.runtime_image_receipt import RuntimeImageReceipt # noqa: PLC0415
        from ..models.runtime_image_reference_intent import RuntimeImageReferenceIntent # noqa: PLC0415
        force_rebuild = self.force_rebuild

        kind = self.kind

        progress = self.progress.to_dict()

        recipe = self.recipe.to_dict()

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        request: dict[str, Any]
        if isinstance(self.request, RecipeSelectorIntent):
            request = self.request.to_dict()
        elif isinstance(self.request, RecipeRevisionIntent):
            request = self.request.to_dict()
        else:
            request = self.request.to_dict()


        retry = self.retry.to_dict()

        runtime = self.runtime.to_dict()

        schema_version = self.schema_version

        blockers: list[dict[str, Any]] | None | Unset
        if isinstance(self.blockers, Unset):
            blockers = UNSET
        elif isinstance(self.blockers, list):
            blockers = []
            for blockers_type_0_item_data in self.blockers:
                blockers_type_0_item = blockers_type_0_item_data.to_dict()
                blockers.append(blockers_type_0_item)


        else:
            blockers = self.blockers

        build_dependency: dict[str, Any] | None | Unset
        if isinstance(self.build_dependency, Unset):
            build_dependency = UNSET
        elif isinstance(self.build_dependency, RecipeBuildDependency):
            build_dependency = self.build_dependency.to_dict()
        else:
            build_dependency = self.build_dependency

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, RecipeOperationCancellationResult):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        claim_owner: None | str | Unset
        if isinstance(self.claim_owner, Unset):
            claim_owner = UNSET
        else:
            claim_owner = self.claim_owner

        claim_until: None | str | Unset
        if isinstance(self.claim_until, Unset):
            claim_until = UNSET
        elif isinstance(self.claim_until, datetime.datetime):
            claim_until = self.claim_until.isoformat()
        else:
            claim_until = self.claim_until

        effective_execution_key: None | str | Unset
        if isinstance(self.effective_execution_key, Unset):
            effective_execution_key = UNSET
        else:
            effective_execution_key = self.effective_execution_key

        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        identity_key: None | str | Unset
        if isinstance(self.identity_key, Unset):
            identity_key = UNSET
        else:
            identity_key = self.identity_key

        image_reference_intent: dict[str, Any] | None | Unset
        if isinstance(self.image_reference_intent, Unset):
            image_reference_intent = UNSET
        elif isinstance(self.image_reference_intent, RuntimeImageReferenceIntent):
            image_reference_intent = self.image_reference_intent.to_dict()
        else:
            image_reference_intent = self.image_reference_intent

        image_result: dict[str, Any] | None | Unset
        if isinstance(self.image_result, Unset):
            image_result = UNSET
        elif isinstance(self.image_result, RuntimeImageReceipt):
            image_result = self.image_result.to_dict()
        else:
            image_result = self.image_result

        model_child: dict[str, Any] | None | Unset
        if isinstance(self.model_child, Unset):
            model_child = UNSET
        elif isinstance(self.model_child, AvailabilityModelChild):
            model_child = self.model_child.to_dict()
        else:
            model_child = self.model_child

        model_digest: None | str | Unset
        if isinstance(self.model_digest, Unset):
            model_digest = UNSET
        else:
            model_digest = self.model_digest

        prebuilt_pull: bool | None | Unset
        if isinstance(self.prebuilt_pull, Unset):
            prebuilt_pull = UNSET
        else:
            prebuilt_pull = self.prebuilt_pull

        removal_fence: None | str | Unset
        if isinstance(self.removal_fence, Unset):
            removal_fence = UNSET
        else:
            removal_fence = self.removal_fence

        retry_after_at: None | str | Unset
        if isinstance(self.retry_after_at, Unset):
            retry_after_at = UNSET
        elif isinstance(self.retry_after_at, datetime.datetime):
            retry_after_at = self.retry_after_at.isoformat()
        else:
            retry_after_at = self.retry_after_at

        stage: Literal['available'] | None | Unset
        if isinstance(self.stage, Unset):
            stage = UNSET
        else:
            stage = self.stage

        supersession: dict[str, Any] | None | Unset
        if isinstance(self.supersession, Unset):
            supersession = UNSET
        elif isinstance(self.supersession, AvailabilitySupersession):
            supersession = self.supersession.to_dict()
        else:
            supersession = self.supersession


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "force_rebuild": force_rebuild,
            "kind": kind,
            "progress": progress,
            "recipe": recipe,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "request": request,
            "retry": retry,
            "runtime": runtime,
            "schema_version": schema_version,
        })
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if build_dependency is not UNSET:
            field_dict["build_dependency"] = build_dependency
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if claim_owner is not UNSET:
            field_dict["claim_owner"] = claim_owner
        if claim_until is not UNSET:
            field_dict["claim_until"] = claim_until
        if effective_execution_key is not UNSET:
            field_dict["effective_execution_key"] = effective_execution_key
        if failure is not UNSET:
            field_dict["failure"] = failure
        if identity_key is not UNSET:
            field_dict["identity_key"] = identity_key
        if image_reference_intent is not UNSET:
            field_dict["image_reference_intent"] = image_reference_intent
        if image_result is not UNSET:
            field_dict["image_result"] = image_result
        if model_child is not UNSET:
            field_dict["model_child"] = model_child
        if model_digest is not UNSET:
            field_dict["model_digest"] = model_digest
        if prebuilt_pull is not UNSET:
            field_dict["prebuilt_pull"] = prebuilt_pull
        if removal_fence is not UNSET:
            field_dict["removal_fence"] = removal_fence
        if retry_after_at is not UNSET:
            field_dict["retry_after_at"] = retry_after_at
        if stage is not UNSET:
            field_dict["stage"] = stage
        if supersession is not UNSET:
            field_dict["supersession"] = supersession

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_model_child import AvailabilityModelChild # noqa: PLC0415
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.availability_retry import AvailabilityRetry # noqa: PLC0415
        from ..models.availability_runtime import AvailabilityRuntime # noqa: PLC0415
        from ..models.availability_supersession import AvailabilitySupersession # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_build_dependency import RecipeBuildDependency # noqa: PLC0415
        from ..models.recipe_definition import RecipeDefinition # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_retry_intent import RecipeRetryIntent # noqa: PLC0415
        from ..models.recipe_revision_intent import RecipeRevisionIntent # noqa: PLC0415
        from ..models.recipe_selector_intent import RecipeSelectorIntent # noqa: PLC0415
        from ..models.runtime_image_receipt import RuntimeImageReceipt # noqa: PLC0415
        from ..models.runtime_image_reference_intent import RuntimeImageReferenceIntent # noqa: PLC0415
        d = dict(src_dict)
        force_rebuild = d.pop("force_rebuild")

        kind = cast(Literal['recipe.image.availability.v2'] , d.pop("kind"))
        if kind != 'recipe.image.availability.v2':
            raise ValueError(f"kind must match const 'recipe.image.availability.v2', got '{kind}'")

        progress = OperationProgress.from_dict(d.pop("progress"))




        recipe = RecipeDefinition.from_dict(d.pop("recipe"))




        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        def _parse_request(data: object) -> RecipeRetryIntent | RecipeRevisionIntent | RecipeSelectorIntent:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                request_type_0 = RecipeSelectorIntent.from_dict(data)



                return request_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                request_type_1 = RecipeRevisionIntent.from_dict(data)



                return request_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            request_type_2 = RecipeRetryIntent.from_dict(data)



            return request_type_2

        request = _parse_request(d.pop("request"))


        retry = AvailabilityRetry.from_dict(d.pop("retry"))




        runtime = AvailabilityRuntime.from_dict(d.pop("runtime"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_blockers(data: object) -> list[OperationBlocker] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                blockers_type_0 = []
                _blockers_type_0 = data
                for blockers_type_0_item_data in (_blockers_type_0):
                    blockers_type_0_item = OperationBlocker.from_dict(blockers_type_0_item_data)



                    blockers_type_0.append(blockers_type_0_item)

                return blockers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[OperationBlocker] | None | Unset, data)

        blockers = _parse_blockers(d.pop("blockers", UNSET))


        def _parse_build_dependency(data: object) -> None | RecipeBuildDependency | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                build_dependency_type_0 = RecipeBuildDependency.from_dict(data)



                return build_dependency_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeBuildDependency | Unset, data)

        build_dependency = _parse_build_dependency(d.pop("build_dependency", UNSET))


        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_cancellation(data: object) -> None | RecipeOperationCancellationResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = RecipeOperationCancellationResult.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationCancellationResult | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        def _parse_claim_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claim_owner = _parse_claim_owner(d.pop("claim_owner", UNSET))


        def _parse_claim_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                claim_until_type_0 = datetime.datetime.fromisoformat(data)



                return claim_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        claim_until = _parse_claim_until(d.pop("claim_until", UNSET))


        def _parse_effective_execution_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effective_execution_key = _parse_effective_execution_key(d.pop("effective_execution_key", UNSET))


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


        def _parse_identity_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        identity_key = _parse_identity_key(d.pop("identity_key", UNSET))


        def _parse_image_reference_intent(data: object) -> None | RuntimeImageReferenceIntent | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                image_reference_intent_type_0 = RuntimeImageReferenceIntent.from_dict(data)



                return image_reference_intent_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RuntimeImageReferenceIntent | Unset, data)

        image_reference_intent = _parse_image_reference_intent(d.pop("image_reference_intent", UNSET))


        def _parse_image_result(data: object) -> None | RuntimeImageReceipt | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                image_result_type_0 = RuntimeImageReceipt.from_dict(data)



                return image_result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RuntimeImageReceipt | Unset, data)

        image_result = _parse_image_result(d.pop("image_result", UNSET))


        def _parse_model_child(data: object) -> AvailabilityModelChild | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_child_type_0 = AvailabilityModelChild.from_dict(data)



                return model_child_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityModelChild | None | Unset, data)

        model_child = _parse_model_child(d.pop("model_child", UNSET))


        def _parse_model_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_digest = _parse_model_digest(d.pop("model_digest", UNSET))


        def _parse_prebuilt_pull(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        prebuilt_pull = _parse_prebuilt_pull(d.pop("prebuilt_pull", UNSET))


        def _parse_removal_fence(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        removal_fence = _parse_removal_fence(d.pop("removal_fence", UNSET))


        def _parse_retry_after_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                retry_after_at_type_0 = datetime.datetime.fromisoformat(data)



                return retry_after_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        retry_after_at = _parse_retry_after_at(d.pop("retry_after_at", UNSET))


        def _parse_stage(data: object) -> Literal['available'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            stage_type_0 = cast(Literal['available'] , data)
            if stage_type_0 != 'available':
                raise ValueError(f"stage_type_0 must match const 'available', got '{stage_type_0}'")
            return stage_type_0
            return cast(Literal['available'] | None | Unset, data)

        stage = _parse_stage(d.pop("stage", UNSET))


        def _parse_supersession(data: object) -> AvailabilitySupersession | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                supersession_type_0 = AvailabilitySupersession.from_dict(data)



                return supersession_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilitySupersession | None | Unset, data)

        supersession = _parse_supersession(d.pop("supersession", UNSET))


        availability_job_payload = cls(
            force_rebuild=force_rebuild,
            kind=kind,
            progress=progress,
            recipe=recipe,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            request=request,
            retry=retry,
            runtime=runtime,
            schema_version=schema_version,
            blockers=blockers,
            build_dependency=build_dependency,
            build_input_sha256=build_input_sha256,
            cancellation=cancellation,
            claim_owner=claim_owner,
            claim_until=claim_until,
            effective_execution_key=effective_execution_key,
            failure=failure,
            identity_key=identity_key,
            image_reference_intent=image_reference_intent,
            image_result=image_result,
            model_child=model_child,
            model_digest=model_digest,
            prebuilt_pull=prebuilt_pull,
            removal_fence=removal_fence,
            retry_after_at=retry_after_at,
            stage=stage,
            supersession=supersession,
        )

        return availability_job_payload
