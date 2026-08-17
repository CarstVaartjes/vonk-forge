from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="UninstallConsequencesResponse")



@_attrs_define
class UninstallConsequencesResponse:
    """
        Attributes:
            automatic_stop (bool):
            catalog_retained (bool):
            reinstall_required (bool):
     """

    automatic_stop: bool
    catalog_retained: bool
    reinstall_required: bool





    def to_dict(self) -> dict[str, Any]:
        automatic_stop = self.automatic_stop

        catalog_retained = self.catalog_retained

        reinstall_required = self.reinstall_required


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "automatic_stop": automatic_stop,
            "catalog_retained": catalog_retained,
            "reinstall_required": reinstall_required,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        automatic_stop = d.pop("automatic_stop")

        catalog_retained = d.pop("catalog_retained")

        reinstall_required = d.pop("reinstall_required")

        uninstall_consequences_response = cls(
            automatic_stop=automatic_stop,
            catalog_retained=catalog_retained,
            reinstall_required=reinstall_required,
        )

        return uninstall_consequences_response
