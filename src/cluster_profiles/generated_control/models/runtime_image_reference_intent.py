from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RuntimeImageReferenceIntent")



@_attrs_define
class RuntimeImageReferenceIntent:
    """ Exact SQL coordination identity for an image archive publication.

    This intent protects an archive while the existing availability owner is
    publishing it. It is not evidence that managed bytes or a receipt exist;
    those facts remain owned by ``RuntimeImageStorage``.

        Attributes:
            attempt (int):
            claim_owner (str):
            image_bytes (int):
            image_digest (str):
            oci_archive_sha256 (str):
            operation_id (str):
            recipe_revision_id (str):
            schema_version (Literal[2]):
     """

    attempt: int
    claim_owner: str
    image_bytes: int
    image_digest: str
    oci_archive_sha256: str
    operation_id: str
    recipe_revision_id: str
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        attempt = self.attempt

        claim_owner = self.claim_owner

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_archive_sha256 = self.oci_archive_sha256

        operation_id = self.operation_id

        recipe_revision_id = self.recipe_revision_id

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "claim_owner": claim_owner,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_archive_sha256": oci_archive_sha256,
            "operation_id": operation_id,
            "recipe_revision_id": recipe_revision_id,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attempt = d.pop("attempt")

        claim_owner = d.pop("claim_owner")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_archive_sha256 = d.pop("oci_archive_sha256")

        operation_id = d.pop("operation_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        runtime_image_reference_intent = cls(
            attempt=attempt,
            claim_owner=claim_owner,
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_archive_sha256=oci_archive_sha256,
            operation_id=operation_id,
            recipe_revision_id=recipe_revision_id,
            schema_version=schema_version,
        )

        return runtime_image_reference_intent
