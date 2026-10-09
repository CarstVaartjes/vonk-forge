from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_load_review import FleetProfileLoadReview





T = TypeVar("T", bound="FleetProfileLoadRequest")



@_attrs_define
class FleetProfileLoadRequest:
    """
        Attributes:
            request_key (str):
            review (FleetProfileLoadReview | None | Unset):
     """

    request_key: str
    review: FleetProfileLoadReview | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_load_review import FleetProfileLoadReview # noqa: PLC0415
        request_key = self.request_key

        review: dict[str, Any] | None | Unset
        if isinstance(self.review, Unset):
            review = UNSET
        elif isinstance(self.review, FleetProfileLoadReview):
            review = self.review.to_dict()
        else:
            review = self.review


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if review is not UNSET:
            field_dict["review"] = review

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_load_review import FleetProfileLoadReview # noqa: PLC0415
        d = dict(src_dict)
        request_key = d.pop("request_key")

        def _parse_review(data: object) -> FleetProfileLoadReview | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                review_type_0 = FleetProfileLoadReview.from_dict(data)



                return review_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileLoadReview | None | Unset, data)

        review = _parse_review(d.pop("review", UNSET))


        fleet_profile_load_request = cls(
            request_key=request_key,
            review=review,
        )

        return fleet_profile_load_request
