from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="DeploymentModelIdentity")



@_attrs_define
class DeploymentModelIdentity:
    """
        Attributes:
            content_sha256 (str):
            publisher (str):
            repository (str):
            revision (str):
            selection_id (str):
            slug (str):
            artifact_key (Union[None, Unset, str]):
     """

    content_sha256: str
    publisher: str
    repository: str
    revision: str
    selection_id: str
    slug: str
    artifact_key: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        publisher = self.publisher

        repository = self.repository

        revision = self.revision

        selection_id = self.selection_id

        slug = self.slug

        artifact_key: Union[None, Unset, str]
        if isinstance(self.artifact_key, Unset):
            artifact_key = UNSET
        else:
            artifact_key = self.artifact_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "publisher": publisher,
            "repository": repository,
            "revision": revision,
            "selection_id": selection_id,
            "slug": slug,
        })
        if artifact_key is not UNSET:
            field_dict["artifact_key"] = artifact_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        publisher = d.pop("publisher")

        repository = d.pop("repository")

        revision = d.pop("revision")

        selection_id = d.pop("selection_id")

        slug = d.pop("slug")

        def _parse_artifact_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        artifact_key = _parse_artifact_key(d.pop("artifact_key", UNSET))


        deployment_model_identity = cls(
            content_sha256=content_sha256,
            publisher=publisher,
            repository=repository,
            revision=revision,
            selection_id=selection_id,
            slug=slug,
            artifact_key=artifact_key,
        )

        return deployment_model_identity
