from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.compiled_distribution_object import CompiledDistributionObject
  from ..models.compiled_artifact_mount import CompiledArtifactMount
  from ..models.compiled_model_identity import CompiledModelIdentity





T = TypeVar("T", bound="CompiledArtifact")



@_attrs_define
class CompiledArtifact:
    """
        Attributes:
            distribution_object (CompiledDistributionObject): Distribution objects usable as installed model or runtime
                inputs.
            file_id (str):
            model (CompiledModelIdentity):
            mount (CompiledArtifactMount):
            path (str):
            roles (list[str]):
            selection_id (str):
            sha256 (str):
            size_bytes (int):
     """

    distribution_object: 'CompiledDistributionObject'
    file_id: str
    model: 'CompiledModelIdentity'
    mount: 'CompiledArtifactMount'
    path: str
    roles: list[str]
    selection_id: str
    sha256: str
    size_bytes: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_distribution_object import CompiledDistributionObject
        from ..models.compiled_artifact_mount import CompiledArtifactMount
        from ..models.compiled_model_identity import CompiledModelIdentity
        distribution_object = self.distribution_object.to_dict()

        file_id = self.file_id

        model = self.model.to_dict()

        mount = self.mount.to_dict()

        path = self.path

        roles = self.roles



        selection_id = self.selection_id

        sha256 = self.sha256

        size_bytes = self.size_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "distribution_object": distribution_object,
            "file_id": file_id,
            "model": model,
            "mount": mount,
            "path": path,
            "roles": roles,
            "selection_id": selection_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_distribution_object import CompiledDistributionObject
        from ..models.compiled_artifact_mount import CompiledArtifactMount
        from ..models.compiled_model_identity import CompiledModelIdentity
        d = dict(src_dict)
        distribution_object = CompiledDistributionObject.from_dict(d.pop("distribution_object"))




        file_id = d.pop("file_id")

        model = CompiledModelIdentity.from_dict(d.pop("model"))




        mount = CompiledArtifactMount.from_dict(d.pop("mount"))




        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        selection_id = d.pop("selection_id")

        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        compiled_artifact = cls(
            distribution_object=distribution_object,
            file_id=file_id,
            model=model,
            mount=mount,
            path=path,
            roles=roles,
            selection_id=selection_id,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return compiled_artifact
