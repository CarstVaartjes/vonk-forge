from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_access_authentication import check_model_access_authentication
from ..models.model_access_authentication import ModelAccessAuthentication
from ..models.model_access_visibility import check_model_access_visibility
from ..models.model_access_visibility import ModelAccessVisibility
from typing import cast






T = TypeVar("T", bound="ModelAccess")



@_attrs_define
class ModelAccess:
    """
        Attributes:
            authentication (ModelAccessAuthentication):
            gated (bool):
            visibility (ModelAccessVisibility):
     """

    authentication: ModelAccessAuthentication
    gated: bool
    visibility: ModelAccessVisibility





    def to_dict(self) -> dict[str, Any]:
        authentication: str = self.authentication

        gated = self.gated

        visibility: str = self.visibility


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authentication": authentication,
            "gated": gated,
            "visibility": visibility,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authentication = check_model_access_authentication(d.pop("authentication"))




        gated = d.pop("gated")

        visibility = check_model_access_visibility(d.pop("visibility"))




        model_access = cls(
            authentication=authentication,
            gated=gated,
            visibility=visibility,
        )

        return model_access
