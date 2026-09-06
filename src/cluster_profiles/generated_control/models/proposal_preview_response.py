from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ProposalPreviewResponse")



@_attrs_define
class ProposalPreviewResponse:
    """
        Attributes:
            affected_documents (list[str]):
            base_revision (str):
            digest (str):
            patch (str):
            validation_results (list[str]):
     """

    affected_documents: list[str]
    base_revision: str
    digest: str
    patch: str
    validation_results: list[str]





    def to_dict(self) -> dict[str, Any]:
        affected_documents = self.affected_documents



        base_revision = self.base_revision

        digest = self.digest

        patch = self.patch

        validation_results = self.validation_results




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "affected_documents": affected_documents,
            "base_revision": base_revision,
            "digest": digest,
            "patch": patch,
            "validation_results": validation_results,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        affected_documents = cast(list[str], d.pop("affected_documents"))


        base_revision = d.pop("base_revision")

        digest = d.pop("digest")

        patch = d.pop("patch")

        validation_results = cast(list[str], d.pop("validation_results"))


        proposal_preview_response = cls(
            affected_documents=affected_documents,
            base_revision=base_revision,
            digest=digest,
            patch=patch,
            validation_results=validation_results,
        )

        return proposal_preview_response
