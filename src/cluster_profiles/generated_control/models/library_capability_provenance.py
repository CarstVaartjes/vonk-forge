from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_capability_provenance_source_kind import check_library_capability_provenance_source_kind
from ..models.library_capability_provenance_source_kind import LibraryCapabilityProvenanceSourceKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryCapabilityProvenance")



@_attrs_define
class LibraryCapabilityProvenance:
    """ Exact source identity and bounded location for one capability inventory.

        Attributes:
            content_sha256 (Union[None, str]):
            evidence_digest (Union[None, str]):
            path (Union[None, str]):
            publisher (str):
            slug (str):
            source_kind (LibraryCapabilityProvenanceSourceKind):
            revision_id (Union[None, Unset, str]):
     """

    content_sha256: Union[None, str]
    evidence_digest: Union[None, str]
    path: Union[None, str]
    publisher: str
    slug: str
    source_kind: LibraryCapabilityProvenanceSourceKind
    revision_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        content_sha256: Union[None, str]
        content_sha256 = self.content_sha256

        evidence_digest: Union[None, str]
        evidence_digest = self.evidence_digest

        path: Union[None, str]
        path = self.path

        publisher = self.publisher

        slug = self.slug

        source_kind: str = self.source_kind

        revision_id: Union[None, Unset, str]
        if isinstance(self.revision_id, Unset):
            revision_id = UNSET
        else:
            revision_id = self.revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "evidence_digest": evidence_digest,
            "path": path,
            "publisher": publisher,
            "slug": slug,
            "source_kind": source_kind,
        })
        if revision_id is not UNSET:
            field_dict["revision_id"] = revision_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256"))


        def _parse_evidence_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest"))


        def _parse_path(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        path = _parse_path(d.pop("path"))


        publisher = d.pop("publisher")

        slug = d.pop("slug")

        source_kind = check_library_capability_provenance_source_kind(d.pop("source_kind"))




        def _parse_revision_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        revision_id = _parse_revision_id(d.pop("revision_id", UNSET))


        library_capability_provenance = cls(
            content_sha256=content_sha256,
            evidence_digest=evidence_digest,
            path=path,
            publisher=publisher,
            slug=slug,
            source_kind=source_kind,
            revision_id=revision_id,
        )

        return library_capability_provenance
