from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cache_removal_finding_asset_kind import CacheRemovalFindingAssetKind
from ..models.cache_removal_finding_asset_kind import check_cache_removal_finding_asset_kind
from ..models.cache_removal_finding_classification import CacheRemovalFindingClassification
from ..models.cache_removal_finding_classification import check_cache_removal_finding_classification
from typing import cast






T = TypeVar("T", bound="CacheRemovalFinding")



@_attrs_define
class CacheRemovalFinding:
    """ One existing owner predicate's exact reference or active-work finding.

        Attributes:
            asset_kind (CacheRemovalFindingAssetKind):
            asset_sha256 (str):
            classification (CacheRemovalFindingClassification):
            detail (str):
            owner_id (str):
            owner_kind (str):
            reason (str):
            state (str):
     """

    asset_kind: CacheRemovalFindingAssetKind
    asset_sha256: str
    classification: CacheRemovalFindingClassification
    detail: str
    owner_id: str
    owner_kind: str
    reason: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        asset_kind: str = self.asset_kind

        asset_sha256 = self.asset_sha256

        classification: str = self.classification

        detail = self.detail

        owner_id = self.owner_id

        owner_kind = self.owner_kind

        reason = self.reason

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "asset_kind": asset_kind,
            "asset_sha256": asset_sha256,
            "classification": classification,
            "detail": detail,
            "owner_id": owner_id,
            "owner_kind": owner_kind,
            "reason": reason,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        asset_kind = check_cache_removal_finding_asset_kind(d.pop("asset_kind"))




        asset_sha256 = d.pop("asset_sha256")

        classification = check_cache_removal_finding_classification(d.pop("classification"))




        detail = d.pop("detail")

        owner_id = d.pop("owner_id")

        owner_kind = d.pop("owner_kind")

        reason = d.pop("reason")

        state = d.pop("state")

        cache_removal_finding = cls(
            asset_kind=asset_kind,
            asset_sha256=asset_sha256,
            classification=classification,
            detail=detail,
            owner_id=owner_id,
            owner_kind=owner_kind,
            reason=reason,
            state=state,
        )

        return cache_removal_finding
