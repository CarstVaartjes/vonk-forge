from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
from typing import Literal, cast
from uuid import UUID
import datetime

if TYPE_CHECKING:
  from ..models.distribution_object import DistributionObject





T = TypeVar("T", bound="DistributionAssignment")



@_attrs_define
class DistributionAssignment:
    """ Controller authorization for one node, generation and object set.

        Attributes:
            assignment_id (UUID):
            expires_at (datetime.datetime):
            generation (int):
            model_artifact_set_sha256 (str):
            node_id (str):
            objects (list['DistributionObject']):
            oci_archive_sha256 (str):
            oci_image_digest (str):
            plan_digest (str):
            schema_version (Literal[2]):
     """

    assignment_id: UUID
    expires_at: datetime.datetime
    generation: int
    model_artifact_set_sha256: str
    node_id: str
    objects: list['DistributionObject']
    oci_archive_sha256: str
    oci_image_digest: str
    plan_digest: str
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        from ..models.distribution_object import DistributionObject
        assignment_id = str(self.assignment_id)

        expires_at = self.expires_at.isoformat()

        generation = self.generation

        model_artifact_set_sha256 = self.model_artifact_set_sha256

        node_id = self.node_id

        objects = []
        for objects_item_data in self.objects:
            objects_item = objects_item_data.to_dict()
            objects.append(objects_item)



        oci_archive_sha256 = self.oci_archive_sha256

        oci_image_digest = self.oci_image_digest

        plan_digest = self.plan_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignment_id": assignment_id,
            "expires_at": expires_at,
            "generation": generation,
            "model_artifact_set_sha256": model_artifact_set_sha256,
            "node_id": node_id,
            "objects": objects,
            "oci_archive_sha256": oci_archive_sha256,
            "oci_image_digest": oci_image_digest,
            "plan_digest": plan_digest,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.distribution_object import DistributionObject
        d = dict(src_dict)
        assignment_id = UUID(d.pop("assignment_id"))




        expires_at = isoparse(d.pop("expires_at"))




        generation = d.pop("generation")

        model_artifact_set_sha256 = d.pop("model_artifact_set_sha256")

        node_id = d.pop("node_id")

        objects = []
        _objects = d.pop("objects")
        for objects_item_data in (_objects):
            objects_item = DistributionObject.from_dict(objects_item_data)



            objects.append(objects_item)


        oci_archive_sha256 = d.pop("oci_archive_sha256")

        oci_image_digest = d.pop("oci_image_digest")

        plan_digest = d.pop("plan_digest")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        distribution_assignment = cls(
            assignment_id=assignment_id,
            expires_at=expires_at,
            generation=generation,
            model_artifact_set_sha256=model_artifact_set_sha256,
            node_id=node_id,
            objects=objects,
            oci_archive_sha256=oci_archive_sha256,
            oci_image_digest=oci_image_digest,
            plan_digest=plan_digest,
            schema_version=schema_version,
        )

        return distribution_assignment
