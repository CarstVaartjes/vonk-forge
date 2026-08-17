from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.catalog_entity_revision_response_kind import CatalogEntityRevisionResponseKind
from ..models.catalog_entity_revision_response_kind import check_catalog_entity_revision_response_kind
from ..models.catalog_entity_revision_response_lifecycle import CatalogEntityRevisionResponseLifecycle
from ..models.catalog_entity_revision_response_lifecycle import check_catalog_entity_revision_response_lifecycle
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.catalog_entity_revision_response_document import CatalogEntityRevisionResponseDocument





T = TypeVar("T", bound="CatalogEntityRevisionResponse")



@_attrs_define
class CatalogEntityRevisionResponse:
    """
        Attributes:
            created_at (str):
            created_by (str):
            document (CatalogEntityRevisionResponseDocument):
            entity_id (str):
            kind (CatalogEntityRevisionResponseKind):
            lifecycle (CatalogEntityRevisionResponseLifecycle):
            publisher (str):
            revision_id (str):
            revision_number (int):
            schema_version (Literal[1]):
            slug (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    created_at: str
    created_by: str
    document: 'CatalogEntityRevisionResponseDocument'
    entity_id: str
    kind: CatalogEntityRevisionResponseKind
    lifecycle: CatalogEntityRevisionResponseLifecycle
    publisher: str
    revision_id: str
    revision_number: int
    schema_version: Literal[1]
    slug: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.catalog_entity_revision_response_document import CatalogEntityRevisionResponseDocument
        created_at = self.created_at

        created_by = self.created_by

        document = self.document.to_dict()

        entity_id = self.entity_id

        kind: str = self.kind

        lifecycle: str = self.lifecycle

        publisher = self.publisher

        revision_id = self.revision_id

        revision_number = self.revision_number

        schema_version = self.schema_version

        slug = self.slug

        title = self.title

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "created_at": created_at,
            "created_by": created_by,
            "document": document,
            "entity_id": entity_id,
            "kind": kind,
            "lifecycle": lifecycle,
            "publisher": publisher,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "schema_version": schema_version,
            "slug": slug,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.catalog_entity_revision_response_document import CatalogEntityRevisionResponseDocument
        d = dict(src_dict)
        created_at = d.pop("created_at")

        created_by = d.pop("created_by")

        document = CatalogEntityRevisionResponseDocument.from_dict(d.pop("document"))




        entity_id = d.pop("entity_id")

        kind = check_catalog_entity_revision_response_kind(d.pop("kind"))




        lifecycle = check_catalog_entity_revision_response_lifecycle(d.pop("lifecycle"))




        publisher = d.pop("publisher")

        revision_id = d.pop("revision_id")

        revision_number = d.pop("revision_number")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        catalog_entity_revision_response = cls(
            created_at=created_at,
            created_by=created_by,
            document=document,
            entity_id=entity_id,
            kind=kind,
            lifecycle=lifecycle,
            publisher=publisher,
            revision_id=revision_id,
            revision_number=revision_number,
            schema_version=schema_version,
            slug=slug,
            title=title,
            content_sha256=content_sha256,
        )

        return catalog_entity_revision_response
