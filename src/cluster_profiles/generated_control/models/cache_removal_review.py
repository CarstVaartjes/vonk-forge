from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cache_removal_review_resource_kind import CacheRemovalReviewResourceKind
from ..models.cache_removal_review_resource_kind import check_cache_removal_review_resource_kind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.cache_removal_asset import CacheRemovalAsset
  from ..models.cache_removal_finding import CacheRemovalFinding
  from ..models.cache_removal_blocker import CacheRemovalBlocker





T = TypeVar("T", bound="CacheRemovalReview")



@_attrs_define
class CacheRemovalReview:
    """ A complete review whose digest is verified during model validation.

        Attributes:
            active_work (list['CacheRemovalFinding']):
            assets (list['CacheRemovalAsset']):
            blockers (list['CacheRemovalBlocker']):
            observed_at (str):
            references (list['CacheRemovalFinding']):
            resource_kind (CacheRemovalReviewResourceKind):
            review_digest (str):
            selector (str):
            target_identity (str):
            with_model (Union[None, bool]):
            action (Union[Literal['remove'], Unset]):  Default: 'remove'.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    active_work: list['CacheRemovalFinding']
    assets: list['CacheRemovalAsset']
    blockers: list['CacheRemovalBlocker']
    observed_at: str
    references: list['CacheRemovalFinding']
    resource_kind: CacheRemovalReviewResourceKind
    review_digest: str
    selector: str
    target_identity: str
    with_model: Union[None, bool]
    action: Union[Literal['remove'], Unset] = 'remove'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.cache_removal_asset import CacheRemovalAsset
        from ..models.cache_removal_finding import CacheRemovalFinding
        from ..models.cache_removal_blocker import CacheRemovalBlocker
        active_work = []
        for active_work_item_data in self.active_work:
            active_work_item = active_work_item_data.to_dict()
            active_work.append(active_work_item)



        assets = []
        for assets_item_data in self.assets:
            assets_item = assets_item_data.to_dict()
            assets.append(assets_item)



        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        observed_at = self.observed_at

        references = []
        for references_item_data in self.references:
            references_item = references_item_data.to_dict()
            references.append(references_item)



        resource_kind: str = self.resource_kind

        review_digest = self.review_digest

        selector = self.selector

        target_identity = self.target_identity

        with_model: Union[None, bool]
        with_model = self.with_model

        action = self.action

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_work": active_work,
            "assets": assets,
            "blockers": blockers,
            "observed_at": observed_at,
            "references": references,
            "resource_kind": resource_kind,
            "review_digest": review_digest,
            "selector": selector,
            "target_identity": target_identity,
            "with_model": with_model,
        })
        if action is not UNSET:
            field_dict["action"] = action
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cache_removal_asset import CacheRemovalAsset
        from ..models.cache_removal_finding import CacheRemovalFinding
        from ..models.cache_removal_blocker import CacheRemovalBlocker
        d = dict(src_dict)
        active_work = []
        _active_work = d.pop("active_work")
        for active_work_item_data in (_active_work):
            active_work_item = CacheRemovalFinding.from_dict(active_work_item_data)



            active_work.append(active_work_item)


        assets = []
        _assets = d.pop("assets")
        for assets_item_data in (_assets):
            assets_item = CacheRemovalAsset.from_dict(assets_item_data)



            assets.append(assets_item)


        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = CacheRemovalBlocker.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        observed_at = d.pop("observed_at")

        references = []
        _references = d.pop("references")
        for references_item_data in (_references):
            references_item = CacheRemovalFinding.from_dict(references_item_data)



            references.append(references_item)


        resource_kind = check_cache_removal_review_resource_kind(d.pop("resource_kind"))




        review_digest = d.pop("review_digest")

        selector = d.pop("selector")

        target_identity = d.pop("target_identity")

        def _parse_with_model(data: object) -> Union[None, bool]:
            if data is None:
                return data
            return cast(Union[None, bool], data)

        with_model = _parse_with_model(d.pop("with_model"))


        action = cast(Union[Literal['remove'], Unset] , d.pop("action", UNSET))
        if action != 'remove' and not isinstance(action, Unset):
            raise ValueError(f"action must match const 'remove', got '{action}'")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        cache_removal_review = cls(
            active_work=active_work,
            assets=assets,
            blockers=blockers,
            observed_at=observed_at,
            references=references,
            resource_kind=resource_kind,
            review_digest=review_digest,
            selector=selector,
            target_identity=target_identity,
            with_model=with_model,
            action=action,
            schema_version=schema_version,
        )

        return cache_removal_review
