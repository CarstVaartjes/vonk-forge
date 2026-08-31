from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.public_recipe_disk_requirements import PublicRecipeDiskRequirements





T = TypeVar("T", bound="PublicRecipeTopologyRole")



@_attrs_define
class PublicRecipeTopologyRole:
    """
        Attributes:
            count (int):
            disk (PublicRecipeDiskRequirements):
            endpoint_owner (bool):
            name (str):
     """

    count: int
    disk: 'PublicRecipeDiskRequirements'
    endpoint_owner: bool
    name: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_recipe_disk_requirements import PublicRecipeDiskRequirements
        count = self.count

        disk = self.disk.to_dict()

        endpoint_owner = self.endpoint_owner

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "count": count,
            "disk": disk,
            "endpoint_owner": endpoint_owner,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.public_recipe_disk_requirements import PublicRecipeDiskRequirements
        d = dict(src_dict)
        count = d.pop("count")

        disk = PublicRecipeDiskRequirements.from_dict(d.pop("disk"))




        endpoint_owner = d.pop("endpoint_owner")

        name = d.pop("name")

        public_recipe_topology_role = cls(
            count=count,
            disk=disk,
            endpoint_owner=endpoint_owner,
            name=name,
        )

        return public_recipe_topology_role
