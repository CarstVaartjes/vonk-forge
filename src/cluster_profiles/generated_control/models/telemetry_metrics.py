from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.telemetry_provenance import TelemetryProvenance
  from ..models.telemetry_capability import TelemetryCapability
  from ..models.telemetry_runtime import TelemetryRuntime
  from ..models.telemetry_series import TelemetrySeries
  from ..models.telemetry_workload import TelemetryWorkload





T = TypeVar("T", bound="TelemetryMetrics")



@_attrs_define
class TelemetryMetrics:
    """
        Attributes:
            capabilities (list['TelemetryCapability']):
            provenance (TelemetryProvenance):
            runtimes (list['TelemetryRuntime']):
            schema_version (Literal[2]):
            series (list['TelemetrySeries']):
            workloads (list['TelemetryWorkload']):
     """

    capabilities: list['TelemetryCapability']
    provenance: 'TelemetryProvenance'
    runtimes: list['TelemetryRuntime']
    schema_version: Literal[2]
    series: list['TelemetrySeries']
    workloads: list['TelemetryWorkload']





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_provenance import TelemetryProvenance
        from ..models.telemetry_capability import TelemetryCapability
        from ..models.telemetry_runtime import TelemetryRuntime
        from ..models.telemetry_series import TelemetrySeries
        from ..models.telemetry_workload import TelemetryWorkload
        capabilities = []
        for capabilities_item_data in self.capabilities:
            capabilities_item = capabilities_item_data.to_dict()
            capabilities.append(capabilities_item)



        provenance = self.provenance.to_dict()

        runtimes = []
        for runtimes_item_data in self.runtimes:
            runtimes_item = runtimes_item_data.to_dict()
            runtimes.append(runtimes_item)



        schema_version = self.schema_version

        series = []
        for series_item_data in self.series:
            series_item = series_item_data.to_dict()
            series.append(series_item)



        workloads = []
        for workloads_item_data in self.workloads:
            workloads_item = workloads_item_data.to_dict()
            workloads.append(workloads_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "provenance": provenance,
            "runtimes": runtimes,
            "schema_version": schema_version,
            "series": series,
            "workloads": workloads,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_provenance import TelemetryProvenance
        from ..models.telemetry_capability import TelemetryCapability
        from ..models.telemetry_runtime import TelemetryRuntime
        from ..models.telemetry_series import TelemetrySeries
        from ..models.telemetry_workload import TelemetryWorkload
        d = dict(src_dict)
        capabilities = []
        _capabilities = d.pop("capabilities")
        for capabilities_item_data in (_capabilities):
            capabilities_item = TelemetryCapability.from_dict(capabilities_item_data)



            capabilities.append(capabilities_item)


        provenance = TelemetryProvenance.from_dict(d.pop("provenance"))




        runtimes = []
        _runtimes = d.pop("runtimes")
        for runtimes_item_data in (_runtimes):
            runtimes_item = TelemetryRuntime.from_dict(runtimes_item_data)



            runtimes.append(runtimes_item)


        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        series = []
        _series = d.pop("series")
        for series_item_data in (_series):
            series_item = TelemetrySeries.from_dict(series_item_data)



            series.append(series_item)


        workloads = []
        _workloads = d.pop("workloads")
        for workloads_item_data in (_workloads):
            workloads_item = TelemetryWorkload.from_dict(workloads_item_data)



            workloads.append(workloads_item)


        telemetry_metrics = cls(
            capabilities=capabilities,
            provenance=provenance,
            runtimes=runtimes,
            schema_version=schema_version,
            series=series,
            workloads=workloads,
        )

        return telemetry_metrics
