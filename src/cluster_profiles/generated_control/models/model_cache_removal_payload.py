from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.model_cache_access_recheck import ModelCacheAccessRecheck
  from ..models.model_cache_cancellation import ModelCacheCancellation
  from ..models.model_cache_claim import ModelCacheClaim
  from ..models.model_cache_removal_result import ModelCacheRemovalResult
  from ..models.model_cache_retry import ModelCacheRetry
  from ..models.operation_blocker import OperationBlocker





T = TypeVar("T", bound="ModelCacheRemovalPayload")



@_attrs_define
class ModelCacheRemovalPayload:
    """ Exact, restartable removal plan and its durable effect checkpoint.

        Attributes:
            delete_objects (list[str]):
            object_index (int):
            object_pending_bytes (int | None):
            reclaimed_bytes (int):
            removal_fence (str):
            result (ModelCacheRemovalResult | None):
            retry (ModelCacheRetry):
            review_digest (None | str):
            schema_version (Literal[2]):
            selected (list[str]):
            selected_objects (list[str]):
            selector (str):
            set_index (int):
            source_policy (Literal['nas-first']):
            access_recheck (ModelCacheAccessRecheck | None | Unset):
            blockers (list[OperationBlocker] | Unset):
            cancellation (ModelCacheCancellation | None | Unset):
            claim (ModelCacheClaim | None | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            force_refresh (bool | Unset):  Default: False.
            model_content_sha256 (None | str | Unset):
            operator_action (None | str | Unset):
            resume_of (None | str | Unset):
            retry_of (None | str | Unset):
            with_model (bool | None | Unset):
     """

    delete_objects: list[str]
    object_index: int
    object_pending_bytes: int | None
    reclaimed_bytes: int
    removal_fence: str
    result: ModelCacheRemovalResult | None
    retry: ModelCacheRetry
    review_digest: None | str
    schema_version: Literal[2]
    selected: list[str]
    selected_objects: list[str]
    selector: str
    set_index: int
    source_policy: Literal['nas-first']
    access_recheck: ModelCacheAccessRecheck | None | Unset = UNSET
    blockers: list[OperationBlocker] | Unset = UNSET
    cancellation: ModelCacheCancellation | None | Unset = UNSET
    claim: ModelCacheClaim | None | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    force_refresh: bool | Unset = False
    model_content_sha256: None | str | Unset = UNSET
    operator_action: None | str | Unset = UNSET
    resume_of: None | str | Unset = UNSET
    retry_of: None | str | Unset = UNSET
    with_model: bool | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.model_cache_access_recheck import ModelCacheAccessRecheck # noqa: PLC0415
        from ..models.model_cache_cancellation import ModelCacheCancellation # noqa: PLC0415
        from ..models.model_cache_claim import ModelCacheClaim # noqa: PLC0415
        from ..models.model_cache_removal_result import ModelCacheRemovalResult # noqa: PLC0415
        from ..models.model_cache_retry import ModelCacheRetry # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        delete_objects = self.delete_objects



        object_index = self.object_index

        object_pending_bytes: int | None
        object_pending_bytes = self.object_pending_bytes

        reclaimed_bytes = self.reclaimed_bytes

        removal_fence = self.removal_fence

        result: dict[str, Any] | None
        if isinstance(self.result, ModelCacheRemovalResult):
            result = self.result.to_dict()
        else:
            result = self.result

        retry = self.retry.to_dict()

        review_digest: None | str
        review_digest = self.review_digest

        schema_version = self.schema_version

        selected = self.selected



        selected_objects = self.selected_objects



        selector = self.selector

        set_index = self.set_index

        source_policy = self.source_policy

        access_recheck: dict[str, Any] | None | Unset
        if isinstance(self.access_recheck, Unset):
            access_recheck = UNSET
        elif isinstance(self.access_recheck, ModelCacheAccessRecheck):
            access_recheck = self.access_recheck.to_dict()
        else:
            access_recheck = self.access_recheck

        blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, ModelCacheCancellation):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        claim: dict[str, Any] | None | Unset
        if isinstance(self.claim, Unset):
            claim = UNSET
        elif isinstance(self.claim, ModelCacheClaim):
            claim = self.claim.to_dict()
        else:
            claim = self.claim

        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        force_refresh = self.force_refresh

        model_content_sha256: None | str | Unset
        if isinstance(self.model_content_sha256, Unset):
            model_content_sha256 = UNSET
        else:
            model_content_sha256 = self.model_content_sha256

        operator_action: None | str | Unset
        if isinstance(self.operator_action, Unset):
            operator_action = UNSET
        else:
            operator_action = self.operator_action

        resume_of: None | str | Unset
        if isinstance(self.resume_of, Unset):
            resume_of = UNSET
        else:
            resume_of = self.resume_of

        retry_of: None | str | Unset
        if isinstance(self.retry_of, Unset):
            retry_of = UNSET
        else:
            retry_of = self.retry_of

        with_model: bool | None | Unset
        if isinstance(self.with_model, Unset):
            with_model = UNSET
        else:
            with_model = self.with_model


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "delete_objects": delete_objects,
            "object_index": object_index,
            "object_pending_bytes": object_pending_bytes,
            "reclaimed_bytes": reclaimed_bytes,
            "removal_fence": removal_fence,
            "result": result,
            "retry": retry,
            "review_digest": review_digest,
            "schema_version": schema_version,
            "selected": selected,
            "selected_objects": selected_objects,
            "selector": selector,
            "set_index": set_index,
            "source_policy": source_policy,
        })
        if access_recheck is not UNSET:
            field_dict["access_recheck"] = access_recheck
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if claim is not UNSET:
            field_dict["claim"] = claim
        if failure is not UNSET:
            field_dict["failure"] = failure
        if force_refresh is not UNSET:
            field_dict["force_refresh"] = force_refresh
        if model_content_sha256 is not UNSET:
            field_dict["model_content_sha256"] = model_content_sha256
        if operator_action is not UNSET:
            field_dict["operator_action"] = operator_action
        if resume_of is not UNSET:
            field_dict["resume_of"] = resume_of
        if retry_of is not UNSET:
            field_dict["retry_of"] = retry_of
        if with_model is not UNSET:
            field_dict["with_model"] = with_model

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.model_cache_access_recheck import ModelCacheAccessRecheck # noqa: PLC0415
        from ..models.model_cache_cancellation import ModelCacheCancellation # noqa: PLC0415
        from ..models.model_cache_claim import ModelCacheClaim # noqa: PLC0415
        from ..models.model_cache_removal_result import ModelCacheRemovalResult # noqa: PLC0415
        from ..models.model_cache_retry import ModelCacheRetry # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        d = dict(src_dict)
        delete_objects = cast(list[str], d.pop("delete_objects"))


        object_index = d.pop("object_index")

        def _parse_object_pending_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        object_pending_bytes = _parse_object_pending_bytes(d.pop("object_pending_bytes"))


        reclaimed_bytes = d.pop("reclaimed_bytes")

        removal_fence = d.pop("removal_fence")

        def _parse_result(data: object) -> ModelCacheRemovalResult | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ModelCacheRemovalResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheRemovalResult | None, data)

        result = _parse_result(d.pop("result"))


        retry = ModelCacheRetry.from_dict(d.pop("retry"))




        def _parse_review_digest(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        review_digest = _parse_review_digest(d.pop("review_digest"))


        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        selected = cast(list[str], d.pop("selected"))


        selected_objects = cast(list[str], d.pop("selected_objects"))


        selector = d.pop("selector")

        set_index = d.pop("set_index")

        source_policy = cast(Literal['nas-first'] , d.pop("source_policy"))
        if source_policy != 'nas-first':
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        def _parse_access_recheck(data: object) -> ModelCacheAccessRecheck | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                access_recheck_type_0 = ModelCacheAccessRecheck.from_dict(data)



                return access_recheck_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheAccessRecheck | None | Unset, data)

        access_recheck = _parse_access_recheck(d.pop("access_recheck", UNSET))


        _blockers = d.pop("blockers", UNSET)
        blockers: list[OperationBlocker] | Unset = UNSET
        if _blockers is not UNSET:
            blockers = []
            for blockers_item_data in _blockers:
                blockers_item = OperationBlocker.from_dict(blockers_item_data)



                blockers.append(blockers_item)


        def _parse_cancellation(data: object) -> ModelCacheCancellation | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = ModelCacheCancellation.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheCancellation | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        def _parse_claim(data: object) -> ModelCacheClaim | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                claim_type_0 = ModelCacheClaim.from_dict(data)



                return claim_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheClaim | None | Unset, data)

        claim = _parse_claim(d.pop("claim", UNSET))


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


        force_refresh = d.pop("force_refresh", UNSET)

        def _parse_model_content_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256", UNSET))


        def _parse_operator_action(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operator_action = _parse_operator_action(d.pop("operator_action", UNSET))


        def _parse_resume_of(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resume_of = _parse_resume_of(d.pop("resume_of", UNSET))


        def _parse_retry_of(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_of = _parse_retry_of(d.pop("retry_of", UNSET))


        def _parse_with_model(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        with_model = _parse_with_model(d.pop("with_model", UNSET))


        model_cache_removal_payload = cls(
            delete_objects=delete_objects,
            object_index=object_index,
            object_pending_bytes=object_pending_bytes,
            reclaimed_bytes=reclaimed_bytes,
            removal_fence=removal_fence,
            result=result,
            retry=retry,
            review_digest=review_digest,
            schema_version=schema_version,
            selected=selected,
            selected_objects=selected_objects,
            selector=selector,
            set_index=set_index,
            source_policy=source_policy,
            access_recheck=access_recheck,
            blockers=blockers,
            cancellation=cancellation,
            claim=claim,
            failure=failure,
            force_refresh=force_refresh,
            model_content_sha256=model_content_sha256,
            operator_action=operator_action,
            resume_of=resume_of,
            retry_of=retry_of,
            with_model=with_model,
        )

        return model_cache_removal_payload
