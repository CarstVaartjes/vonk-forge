from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.evidence_age_freshness import check_evidence_age_freshness
from ..models.evidence_age_freshness import EvidenceAgeFreshness
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="EvidenceAge")



@_attrs_define
class EvidenceAge:
    """
        Attributes:
            source (str):
            age_seconds (Union[None, Unset, int]):
            freshness (Union[Unset, EvidenceAgeFreshness]):  Default: 'unknown'.
            observed_at (Union[None, Unset, datetime.datetime]):
     """

    source: str
    age_seconds: Union[None, Unset, int] = UNSET
    freshness: Union[Unset, EvidenceAgeFreshness] = 'unknown'
    observed_at: Union[None, Unset, datetime.datetime] = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        age_seconds: Union[None, Unset, int]
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        freshness: Union[Unset, str] = UNSET
        if not isinstance(self.freshness, Unset):
            freshness = self.freshness


        observed_at: Union[None, Unset, str]
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

        def _parse_age_seconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))


        _freshness = d.pop("freshness", UNSET)
        freshness: Union[Unset, EvidenceAgeFreshness]
        if isinstance(_freshness,  Unset):
            freshness = UNSET
        else:
            freshness = check_evidence_age_freshness(_freshness)




        def _parse_observed_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observed_at_type_0 = isoparse(data)



                return observed_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))


        evidence_age = cls(
            source=source,
            age_seconds=age_seconds,
            freshness=freshness,
            observed_at=observed_at,
        )

        return evidence_age
