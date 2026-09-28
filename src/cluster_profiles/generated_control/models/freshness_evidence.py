from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.freshness_evidence_state import check_freshness_evidence_state
from ..models.freshness_evidence_state import FreshnessEvidenceState
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="FreshnessEvidence")



@_attrs_define
class FreshnessEvidence:
    """
        Attributes:
            source (str):
            state (FreshnessEvidenceState):
            age_seconds (float | None | Unset):
            evidence_digest (None | str | Unset):
            maximum_age_seconds (int | None | Unset):
            observed_at (datetime.datetime | None | Unset):
     """

    source: str
    state: FreshnessEvidenceState
    age_seconds: float | None | Unset = UNSET
    evidence_digest: None | str | Unset = UNSET
    maximum_age_seconds: int | None | Unset = UNSET
    observed_at: datetime.datetime | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        state: str = self.state

        age_seconds: float | None | Unset
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        evidence_digest: None | str | Unset
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest

        maximum_age_seconds: int | None | Unset
        if isinstance(self.maximum_age_seconds, Unset):
            maximum_age_seconds = UNSET
        else:
            maximum_age_seconds = self.maximum_age_seconds

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




        def _parse_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))


        def _parse_evidence_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        def _parse_maximum_age_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        maximum_age_seconds = _parse_maximum_age_seconds(d.pop("maximum_age_seconds", UNSET))


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


        freshness_evidence = cls(
            source=source,
            state=state,
            age_seconds=age_seconds,
            evidence_digest=evidence_digest,
            maximum_age_seconds=maximum_age_seconds,
            observed_at=observed_at,
        )

        return freshness_evidence
