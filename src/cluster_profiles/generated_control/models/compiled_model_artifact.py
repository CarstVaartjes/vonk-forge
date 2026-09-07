from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.execution_mount import ExecutionMount
  from ..models.model_catalog_identity import ModelCatalogIdentity
  from ..models.distribution_object_receipt import DistributionObjectReceipt





T = TypeVar("T", bound="CompiledModelArtifact")



@_attrs_define
class CompiledModelArtifact:
    """ One exact model file selected by the canonical runtime compiler.

        Attributes:
            bytes_ (int):
            distribution_object (DistributionObjectReceipt): A verified immutable object served by the Controller.
            file_id (str):
            id (str):
            materialized_path (str):
            model (ModelCatalogIdentity): Safe model identity used for display and execution evidence.

                Upstream repository and revision fields deliberately do not exist here.
                They remain Controller/cache inputs and are never sent to a Spark.
            mount (ExecutionMount): The platform-owned mount used by one selected model file.
            path (str):
            roles (list[str]):
            selection_id (str):
            sha256 (str):
     """

    bytes_: int
    distribution_object: 'DistributionObjectReceipt'
    file_id: str
    id: str
    materialized_path: str
    model: 'ModelCatalogIdentity'
    mount: 'ExecutionMount'
    path: str
    roles: list[str]
    selection_id: str
    sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.execution_mount import ExecutionMount
        from ..models.model_catalog_identity import ModelCatalogIdentity
        from ..models.distribution_object_receipt import DistributionObjectReceipt
        bytes_ = self.bytes_

        distribution_object = self.distribution_object.to_dict()

        file_id = self.file_id

        id = self.id

        materialized_path = self.materialized_path

        model = self.model.to_dict()

        mount = self.mount.to_dict()

        path = self.path

        roles = self.roles



        selection_id = self.selection_id

        sha256 = self.sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "bytes": bytes_,
            "distribution_object": distribution_object,
            "file_id": file_id,
            "id": id,
            "materialized_path": materialized_path,
            "model": model,
            "mount": mount,
            "path": path,
            "roles": roles,
            "selection_id": selection_id,
            "sha256": sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.execution_mount import ExecutionMount
        from ..models.model_catalog_identity import ModelCatalogIdentity
        from ..models.distribution_object_receipt import DistributionObjectReceipt
        d = dict(src_dict)
        bytes_ = d.pop("bytes")

        distribution_object = DistributionObjectReceipt.from_dict(d.pop("distribution_object"))




        file_id = d.pop("file_id")

        id = d.pop("id")

        materialized_path = d.pop("materialized_path")

        model = ModelCatalogIdentity.from_dict(d.pop("model"))




        mount = ExecutionMount.from_dict(d.pop("mount"))




        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        selection_id = d.pop("selection_id")

        sha256 = d.pop("sha256")

        compiled_model_artifact = cls(
            bytes_=bytes_,
            distribution_object=distribution_object,
            file_id=file_id,
            id=id,
            materialized_path=materialized_path,
            model=model,
            mount=mount,
            path=path,
            roles=roles,
            selection_id=selection_id,
            sha256=sha256,
        )

        return compiled_model_artifact
