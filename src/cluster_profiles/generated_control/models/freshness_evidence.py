from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.freshness_evidence_state import check_freshness_evidence_state
from ..models.freshness_evidence_state import FreshnessEvidenceState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="FreshnessEvidence")



@_attrs_define
class FreshnessEvidence:
    """
        Attributes:
            source (str):
            state (FreshnessEvidenceState):
            age_seconds (Union[None, Unset, float]):
            evidence_digest (Union[None, Unset, str]):
            maximum_age_seconds (Union[None, Unset, int]):
            observed_at (Union[None, Unset, datetime.datetime]):
     """

    source: str
    state: FreshnessEvidenceState
    age_seconds: Union[None, Unset, float] = UNSET
    evidence_digest: Union[None, Unset, str] = UNSET
    maximum_age_seconds: Union[None, Unset, int] = UNSET
    observed_at: Union[None, Unset, datetime.datetime] = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        state: str = self.state

        age_seconds: Union[None, Unset, float]
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        evidence_digest: Union[None, Unset, str]
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest

        maximum_age_seconds: Union[None, Unset, int]
        if isinstance(self.maximum_age_seconds, Unset):
            maximum_age_seconds = UNSET
        else:
            maximum_age_seconds = self.maximum_age_seconds

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
            "state": state,
        })
        if age_seconds is not UNSET:
            field_dict["age_seconds"] = age_seconds
        if evidence_digest is not UNSET:
            field_dict["evidence_digest"] = evidence_digest
        if maximum_age_seconds is not UNSET:
            field_dict["maximum_age_seconds"] = maximum_age_seconds
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        state = check_freshness_evidence_state(d.pop("state"))




        def _parse_age_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))


        def _parse_evidence_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        def _parse_maximum_age_seconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        maximum_age_seconds = _parse_maximum_age_seconds(d.pop("maximum_age_seconds", UNSET))


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


        freshness_evidence = cls(
            source=source,
            state=state,
            age_seconds=age_seconds,
            evidence_digest=evidence_digest,
            maximum_age_seconds=maximum_age_seconds,
            observed_at=observed_at,
        )

        return freshness_evidence
