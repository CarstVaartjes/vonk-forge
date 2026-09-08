from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="RecipeLibraryEvidence")



@_attrs_define
class RecipeLibraryEvidence:
    """
        Attributes:
            evidence (EvidenceAge):
            state (str):
            repository (Union[None, Unset, str]):
            source_commit (Union[None, Unset, str]):
     """

    evidence: 'EvidenceAge'
    state: str
    repository: Union[None, Unset, str] = UNSET
    source_commit: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge
        evidence = self.evidence.to_dict()

        state = self.state

        repository: Union[None, Unset, str]
        if isinstance(self.repository, Unset):
            repository = UNSET
        else:
            repository = self.repository

        source_commit: Union[None, Unset, str]
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
        from ..models.evidence_age import EvidenceAge
        d = dict(src_dict)
        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        state = d.pop("state")

        def _parse_repository(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        repository = _parse_repository(d.pop("repository", UNSET))


        def _parse_source_commit(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        source_commit = _parse_source_commit(d.pop("source_commit", UNSET))


        recipe_library_evidence = cls(
            evidence=evidence,
            state=state,
            repository=repository,
            source_commit=source_commit,
        )

        return recipe_library_evidence
