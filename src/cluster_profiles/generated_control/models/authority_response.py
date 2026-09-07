from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.authority_response_documents import AuthorityResponseDocuments
  from ..models.authority_response_dependencies import AuthorityResponseDependencies





T = TypeVar("T", bound="AuthorityResponse")



@_attrs_define
class AuthorityResponse:
    """
        Attributes:
            dependencies (AuthorityResponseDependencies):
            documents (AuthorityResponseDocuments):
            revision (str):
     """

    dependencies: 'AuthorityResponseDependencies'
    documents: 'AuthorityResponseDocuments'
    revision: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.authority_response_documents import AuthorityResponseDocuments
        from ..models.authority_response_dependencies import AuthorityResponseDependencies
        dependencies = self.dependencies.to_dict()

        documents = self.documents.to_dict()

        revision = self.revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "dependencies": dependencies,
            "documents": documents,
            "revision": revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.authority_response_documents import AuthorityResponseDocuments
        from ..models.authority_response_dependencies import AuthorityResponseDependencies
        d = dict(src_dict)
        dependencies = AuthorityResponseDependencies.from_dict(d.pop("dependencies"))




        documents = AuthorityResponseDocuments.from_dict(d.pop("documents"))




        revision = d.pop("revision")

        authority_response = cls(
            dependencies=dependencies,
            documents=documents,
            revision=revision,
        )

        return authority_response
