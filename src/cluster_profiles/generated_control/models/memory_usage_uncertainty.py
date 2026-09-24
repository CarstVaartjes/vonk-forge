from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.run_memory_residual_range import RunMemoryResidualRange





T = TypeVar("T", bound="MemoryUsageUncertainty")



@_attrs_define
class MemoryUsageUncertainty:
    """ Fresh aggregate capacity lacks per-run resident usage evidence.

        Attributes:
            inventory_evidence_digest (str):
            inventory_observed_at (datetime.datetime):
            residual_ranges (list['RunMemoryResidualRange']):
            source (Literal['aggregate_inventory_without_run_usage']):
     """

    inventory_evidence_digest: str
    inventory_observed_at: datetime.datetime
    residual_ranges: list['RunMemoryResidualRange']
    source: Literal['aggregate_inventory_without_run_usage']





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_memory_residual_range import RunMemoryResidualRange
        inventory_evidence_digest = self.inventory_evidence_digest

        inventory_observed_at = self.inventory_observed_at.isoformat()

        residual_ranges = []
        for residual_ranges_item_data in self.residual_ranges:
            residual_ranges_item = residual_ranges_item_data.to_dict()
            residual_ranges.append(residual_ranges_item)



        source = self.source


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "inventory_evidence_digest": inventory_evidence_digest,
            "inventory_observed_at": inventory_observed_at,
            "residual_ranges": residual_ranges,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_memory_residual_range import RunMemoryResidualRange
        d = dict(src_dict)
        inventory_evidence_digest = d.pop("inventory_evidence_digest")

        inventory_observed_at = isoparse(d.pop("inventory_observed_at"))




        residual_ranges = []
        _residual_ranges = d.pop("residual_ranges")
        for residual_ranges_item_data in (_residual_ranges):
            residual_ranges_item = RunMemoryResidualRange.from_dict(residual_ranges_item_data)



            residual_ranges.append(residual_ranges_item)


        source = cast(Literal['aggregate_inventory_without_run_usage'] , d.pop("source"))
        if source != 'aggregate_inventory_without_run_usage':
            raise ValueError(f"source must match const 'aggregate_inventory_without_run_usage', got '{source}'")

        memory_usage_uncertainty = cls(
            inventory_evidence_digest=inventory_evidence_digest,
            inventory_observed_at=inventory_observed_at,
            residual_ranges=residual_ranges,
            source=source,
        )

        return memory_usage_uncertainty
