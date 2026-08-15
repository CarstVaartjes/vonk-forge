from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.visual_catalog_identity import VisualCatalogIdentity





T = TypeVar("T", bound="VisualExecution")



@_attrs_define
class VisualExecution:
    """
        Attributes:
            harness (VisualCatalogIdentity):
            patch_bundle (Union['VisualCatalogIdentity', None]):
     """

    harness: 'VisualCatalogIdentity'
    patch_bundle: Union['VisualCatalogIdentity', None]





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        harness = self.harness.to_dict()

        patch_bundle: Union[None, dict[str, Any]]
        if isinstance(self.patch_bundle, VisualCatalogIdentity):
            patch_bundle = self.patch_bundle.to_dict()
        else:
            patch_bundle = self.patch_bundle


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "harness": harness,
            "patch_bundle": patch_bundle,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        d = dict(src_dict)
        harness = VisualCatalogIdentity.from_dict(d.pop("harness"))




        def _parse_patch_bundle(data: object) -> Union['VisualCatalogIdentity', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                patch_bundle_type_0 = VisualCatalogIdentity.from_dict(data)



                return patch_bundle_type_0
            except: # noqa: E722
                pass
            return cast(Union['VisualCatalogIdentity', None], data)

        patch_bundle = _parse_patch_bundle(d.pop("patch_bundle"))


        visual_execution = cls(
            harness=harness,
            patch_bundle=patch_bundle,
        )

        return visual_execution
