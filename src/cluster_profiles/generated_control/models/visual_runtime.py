from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.visual_catalog_identity import VisualCatalogIdentity





T = TypeVar("T", bound="VisualRuntime")



@_attrs_define
class VisualRuntime:
    """
        Attributes:
            distribution (VisualCatalogIdentity):
            entrypoint (list[str]):
            lifecycle_post_stop_count (int):
            lifecycle_pre_start_count (int):
            stop_timeout_seconds (int):
     """

    distribution: 'VisualCatalogIdentity'
    entrypoint: list[str]
    lifecycle_post_stop_count: int
    lifecycle_pre_start_count: int
    stop_timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        distribution = self.distribution.to_dict()

        entrypoint = self.entrypoint



        lifecycle_post_stop_count = self.lifecycle_post_stop_count

        lifecycle_pre_start_count = self.lifecycle_pre_start_count

        stop_timeout_seconds = self.stop_timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "distribution": distribution,
            "entrypoint": entrypoint,
            "lifecycle_post_stop_count": lifecycle_post_stop_count,
            "lifecycle_pre_start_count": lifecycle_pre_start_count,
            "stop_timeout_seconds": stop_timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_catalog_identity import VisualCatalogIdentity
        d = dict(src_dict)
        distribution = VisualCatalogIdentity.from_dict(d.pop("distribution"))




        entrypoint = cast(list[str], d.pop("entrypoint"))


        lifecycle_post_stop_count = d.pop("lifecycle_post_stop_count")

        lifecycle_pre_start_count = d.pop("lifecycle_pre_start_count")

        stop_timeout_seconds = d.pop("stop_timeout_seconds")

        visual_runtime = cls(
            distribution=distribution,
            entrypoint=entrypoint,
            lifecycle_post_stop_count=lifecycle_post_stop_count,
            lifecycle_pre_start_count=lifecycle_pre_start_count,
            stop_timeout_seconds=stop_timeout_seconds,
        )

        return visual_runtime
