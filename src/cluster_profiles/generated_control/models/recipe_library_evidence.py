from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="RecipeLibraryEvidence")



@_attrs_define
class RecipeLibraryEvidence:
    """
        Attributes:
            evidence (EvidenceAge):
            state (str):
            repository (None | str | Unset):
            source_commit (None | str | Unset):
     """

    evidence: EvidenceAge
    state: str
    repository: None | str | Unset = UNSET
    source_commit: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge # noqa: PLC0415
        evidence = self.evidence.to_dict()

        state = self.state

        repository: None | str | Unset
        if isinstance(self.repository, Unset):
            repository = UNSET
        else:
            repository = self.repository

        source_commit: None | str | Unset
        if isinstance(self.source_commit, Unset):
            source_commit = UNSET
        else:
            source_commit = self.source_commit


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence": evidence,
            "state": state,
        })
        if repository is not UNSET:
            field_dict["repository"] = repository
        if source_commit is not UNSET:
            field_dict["source_commit"] = source_commit

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_age import EvidenceAge # noqa: PLC0415
        d = dict(src_dict)
        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        state = d.pop("state")

        def _parse_repository(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repository = _parse_repository(d.pop("repository", UNSET))


        def _parse_source_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_commit = _parse_source_commit(d.pop("source_commit", UNSET))


        recipe_library_evidence = cls(
            evidence=evidence,
            state=state,
            repository=repository,
            source_commit=source_commit,
        )

        return recipe_library_evidence
