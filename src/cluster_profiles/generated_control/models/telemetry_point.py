from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.telemetry_details import TelemetryDetails
  from ..models.telemetry_metrics import TelemetryMetrics





T = TypeVar("T", bound="TelemetryPoint")



@_attrs_define
class TelemetryPoint:
    """
        Attributes:
            boot_id (str):
            details (TelemetryDetails):
            gap_samples (int):
            id (str):
            node_id (str):
            observed_at (datetime.datetime):
            received_at (datetime.datetime):
            sequence (int):
            cpu_utilization_percent (Union[None, Unset, float]):
            disk_free_bytes (Union[None, Unset, int]):
            disk_total_bytes (Union[None, Unset, int]):
            gpu_memory_free_bytes (Union[None, Unset, int]):
            gpu_memory_total_bytes (Union[None, Unset, int]):
            gpu_utilization_percent (Union[None, Unset, float]):
            load_average_1m (Union[None, Unset, float]):
            memory_available_bytes (Union[None, Unset, int]):
            memory_total_bytes (Union[None, Unset, int]):
            metrics (Union['TelemetryMetrics', None, Unset]):
            network_receive_bytes_per_second (Union[None, Unset, float]):
            network_transmit_bytes_per_second (Union[None, Unset, float]):
            power_watts (Union[None, Unset, float]):
            temperature_c (Union[None, Unset, float]):
     """

    boot_id: str
    details: 'TelemetryDetails'
    gap_samples: int
    id: str
    node_id: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    sequence: int
    cpu_utilization_percent: Union[None, Unset, float] = UNSET
    disk_free_bytes: Union[None, Unset, int] = UNSET
    disk_total_bytes: Union[None, Unset, int] = UNSET
    gpu_memory_free_bytes: Union[None, Unset, int] = UNSET
    gpu_memory_total_bytes: Union[None, Unset, int] = UNSET
    gpu_utilization_percent: Union[None, Unset, float] = UNSET
    load_average_1m: Union[None, Unset, float] = UNSET
    memory_available_bytes: Union[None, Unset, int] = UNSET
    memory_total_bytes: Union[None, Unset, int] = UNSET
    metrics: Union['TelemetryMetrics', None, Unset] = UNSET
    network_receive_bytes_per_second: Union[None, Unset, float] = UNSET
    network_transmit_bytes_per_second: Union[None, Unset, float] = UNSET
    power_watts: Union[None, Unset, float] = UNSET
    temperature_c: Union[None, Unset, float] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_details import TelemetryDetails
        from ..models.telemetry_metrics import TelemetryMetrics
        boot_id = self.boot_id

        details = self.details.to_dict()

        gap_samples = self.gap_samples

        id = self.id

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

        sequence = self.sequence

        cpu_utilization_percent: Union[None, Unset, float]
        if isinstance(self.cpu_utilization_percent, Unset):
            cpu_utilization_percent = UNSET
        else:
            cpu_utilization_percent = self.cpu_utilization_percent

        disk_free_bytes: Union[None, Unset, int]
        if isinstance(self.disk_free_bytes, Unset):
            disk_free_bytes = UNSET
        else:
            disk_free_bytes = self.disk_free_bytes

        disk_total_bytes: Union[None, Unset, int]
        if isinstance(self.disk_total_bytes, Unset):
            disk_total_bytes = UNSET
        else:
            disk_total_bytes = self.disk_total_bytes

        gpu_memory_free_bytes: Union[None, Unset, int]
        if isinstance(self.gpu_memory_free_bytes, Unset):
            gpu_memory_free_bytes = UNSET
        else:
            gpu_memory_free_bytes = self.gpu_memory_free_bytes

        gpu_memory_total_bytes: Union[None, Unset, int]
        if isinstance(self.gpu_memory_total_bytes, Unset):
            gpu_memory_total_bytes = UNSET
        else:
            gpu_memory_total_bytes = self.gpu_memory_total_bytes

        gpu_utilization_percent: Union[None, Unset, float]
        if isinstance(self.gpu_utilization_percent, Unset):
            gpu_utilization_percent = UNSET
        else:
            gpu_utilization_percent = self.gpu_utilization_percent

        load_average_1m: Union[None, Unset, float]
        if isinstance(self.load_average_1m, Unset):
            load_average_1m = UNSET
        else:
            load_average_1m = self.load_average_1m

        memory_available_bytes: Union[None, Unset, int]
        if isinstance(self.memory_available_bytes, Unset):
            memory_available_bytes = UNSET
        else:
            memory_available_bytes = self.memory_available_bytes

        memory_total_bytes: Union[None, Unset, int]
        if isinstance(self.memory_total_bytes, Unset):
            memory_total_bytes = UNSET
        else:
            memory_total_bytes = self.memory_total_bytes

        metrics: Union[None, Unset, dict[str, Any]]
        if isinstance(self.metrics, Unset):
            metrics = UNSET
        elif isinstance(self.metrics, TelemetryMetrics):
            metrics = self.metrics.to_dict()
        else:
            metrics = self.metrics

        network_receive_bytes_per_second: Union[None, Unset, float]
        if isinstance(self.network_receive_bytes_per_second, Unset):
            network_receive_bytes_per_second = UNSET
        else:
            network_receive_bytes_per_second = self.network_receive_bytes_per_second

        network_transmit_bytes_per_second: Union[None, Unset, float]
        if isinstance(self.network_transmit_bytes_per_second, Unset):
            network_transmit_bytes_per_second = UNSET
        else:
            network_transmit_bytes_per_second = self.network_transmit_bytes_per_second

        power_watts: Union[None, Unset, float]
        if isinstance(self.power_watts, Unset):
            power_watts = UNSET
        else:
            power_watts = self.power_watts

        temperature_c: Union[None, Unset, float]
        if isinstance(self.temperature_c, Unset):
            temperature_c = UNSET
        else:
            temperature_c = self.temperature_c


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "boot_id": boot_id,
            "details": details,
            "gap_samples": gap_samples,
            "id": id,
            "node_id": node_id,
            "observed_at": observed_at,
            "received_at": received_at,
            "sequence": sequence,
        })
        if cpu_utilization_percent is not UNSET:
            field_dict["cpu_utilization_percent"] = cpu_utilization_percent
        if disk_free_bytes is not UNSET:
            field_dict["disk_free_bytes"] = disk_free_bytes
        if disk_total_bytes is not UNSET:
            field_dict["disk_total_bytes"] = disk_total_bytes
        if gpu_memory_free_bytes is not UNSET:
            field_dict["gpu_memory_free_bytes"] = gpu_memory_free_bytes
        if gpu_memory_total_bytes is not UNSET:
            field_dict["gpu_memory_total_bytes"] = gpu_memory_total_bytes
        if gpu_utilization_percent is not UNSET:
            field_dict["gpu_utilization_percent"] = gpu_utilization_percent
        if load_average_1m is not UNSET:
            field_dict["load_average_1m"] = load_average_1m
        if memory_available_bytes is not UNSET:
            field_dict["memory_available_bytes"] = memory_available_bytes
        if memory_total_bytes is not UNSET:
            field_dict["memory_total_bytes"] = memory_total_bytes
        if metrics is not UNSET:
            field_dict["metrics"] = metrics
        if network_receive_bytes_per_second is not UNSET:
            field_dict["network_receive_bytes_per_second"] = network_receive_bytes_per_second
        if network_transmit_bytes_per_second is not UNSET:
            field_dict["network_transmit_bytes_per_second"] = network_transmit_bytes_per_second
        if power_watts is not UNSET:
            field_dict["power_watts"] = power_watts
        if temperature_c is not UNSET:
            field_dict["temperature_c"] = temperature_c

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_details import TelemetryDetails
        from ..models.telemetry_metrics import TelemetryMetrics
        d = dict(src_dict)
        boot_id = d.pop("boot_id")

        details = TelemetryDetails.from_dict(d.pop("details"))




        gap_samples = d.pop("gap_samples")

        id = d.pop("id")

        node_id = d.pop("node_id")

        observed_at = isoparse(d.pop("observed_at"))




        received_at = isoparse(d.pop("received_at"))




        sequence = d.pop("sequence")

        def _parse_cpu_utilization_percent(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        cpu_utilization_percent = _parse_cpu_utilization_percent(d.pop("cpu_utilization_percent", UNSET))


        def _parse_disk_free_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_free_bytes = _parse_disk_free_bytes(d.pop("disk_free_bytes", UNSET))


        def _parse_disk_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_total_bytes = _parse_disk_total_bytes(d.pop("disk_total_bytes", UNSET))


        def _parse_gpu_memory_free_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        gpu_memory_free_bytes = _parse_gpu_memory_free_bytes(d.pop("gpu_memory_free_bytes", UNSET))


        def _parse_gpu_memory_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        gpu_memory_total_bytes = _parse_gpu_memory_total_bytes(d.pop("gpu_memory_total_bytes", UNSET))


        def _parse_gpu_utilization_percent(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        gpu_utilization_percent = _parse_gpu_utilization_percent(d.pop("gpu_utilization_percent", UNSET))


        def _parse_load_average_1m(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        load_average_1m = _parse_load_average_1m(d.pop("load_average_1m", UNSET))


        def _parse_memory_available_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_available_bytes = _parse_memory_available_bytes(d.pop("memory_available_bytes", UNSET))


        def _parse_memory_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_total_bytes = _parse_memory_total_bytes(d.pop("memory_total_bytes", UNSET))


        def _parse_metrics(data: object) -> Union['TelemetryMetrics', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                metrics_type_0 = TelemetryMetrics.from_dict(data)



                return metrics_type_0
            except: # noqa: E722
                pass
            return cast(Union['TelemetryMetrics', None, Unset], data)

        metrics = _parse_metrics(d.pop("metrics", UNSET))


        def _parse_network_receive_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        network_receive_bytes_per_second = _parse_network_receive_bytes_per_second(d.pop("network_receive_bytes_per_second", UNSET))


        def _parse_network_transmit_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        network_transmit_bytes_per_second = _parse_network_transmit_bytes_per_second(d.pop("network_transmit_bytes_per_second", UNSET))


        def _parse_power_watts(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        power_watts = _parse_power_watts(d.pop("power_watts", UNSET))


        def _parse_temperature_c(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        temperature_c = _parse_temperature_c(d.pop("temperature_c", UNSET))


        telemetry_point = cls(
            boot_id=boot_id,
            details=details,
            gap_samples=gap_samples,
            id=id,
            node_id=node_id,
            observed_at=observed_at,
            received_at=received_at,
            sequence=sequence,
            cpu_utilization_percent=cpu_utilization_percent,
            disk_free_bytes=disk_free_bytes,
            disk_total_bytes=disk_total_bytes,
            gpu_memory_free_bytes=gpu_memory_free_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            gpu_utilization_percent=gpu_utilization_percent,
            load_average_1m=load_average_1m,
            memory_available_bytes=memory_available_bytes,
            memory_total_bytes=memory_total_bytes,
            metrics=metrics,
            network_receive_bytes_per_second=network_receive_bytes_per_second,
            network_transmit_bytes_per_second=network_transmit_bytes_per_second,
            power_watts=power_watts,
            temperature_c=temperature_c,
        )

        return telemetry_point
