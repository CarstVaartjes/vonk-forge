from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.evidence_age_freshness import check_evidence_age_freshness
from ..models.evidence_age_freshness import EvidenceAgeFreshness
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="EvidenceAge")



@_attrs_define
class EvidenceAge:
    """
        Attributes:
            source (str):
            age_seconds (int | None | Unset):
            freshness (EvidenceAgeFreshness | Unset):  Default: 'unknown'.
            observed_at (datetime.datetime | None | Unset):
     """

    source: str
    age_seconds: int | None | Unset = UNSET
    freshness: EvidenceAgeFreshness | Unset = 'unknown'
    observed_at: datetime.datetime | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        age_seconds: int | None | Unset
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        freshness: str | Unset = UNSET
        if not isinstance(self.freshness, Unset):
            freshness = self.freshness


        observed_at: None | str | Unset
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        elif isinstance(self.observed_at, datetime.datetime):
            observed_at = self.observed_at.isoformat()
        else:
            observed_at = self.observed_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "source": source,
        })
        if age_seconds is not UNSET:
            field_dict["age_seconds"] = age_seconds
        if freshness is not UNSET:
            field_dict["freshness"] = freshness
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        def _parse_age_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))


        _freshness = d.pop("freshness", UNSET)
        freshness: EvidenceAgeFreshness | Unset
        if isinstance(_freshness,  Unset):
            freshness = UNSET
        else:
            freshness = check_evidence_age_freshness(_freshness)




        def _parse_observed_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observed_at_type_0 = datetime.datetime.fromisoformat(data)



                return observed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))


        evidence_age = cls(
            source=source,
            age_seconds=age_seconds,
            freshness=freshness,
            observed_at=observed_at,
        )

        return evidence_age
