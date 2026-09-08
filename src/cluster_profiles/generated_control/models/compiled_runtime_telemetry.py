from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_runtime_telemetry_metrics_format_type_0 import check_compiled_runtime_telemetry_metrics_format_type_0
from ..models.compiled_runtime_telemetry_metrics_format_type_0 import CompiledRuntimeTelemetryMetricsFormatType0
from typing import cast
from typing import cast, Union






T = TypeVar("T", bound="CompiledRuntimeTelemetry")



@_attrs_define
class CompiledRuntimeTelemetry:
    """
        Attributes:
            engine (str):
            engine_version (Union[None, str]):
            metrics_format (Union[CompiledRuntimeTelemetryMetricsFormatType0, None]):
            metrics_path (Union[None, str]):
     """

    engine: str
    engine_version: Union[None, str]
    metrics_format: Union[CompiledRuntimeTelemetryMetricsFormatType0, None]
    metrics_path: Union[None, str]





    def to_dict(self) -> dict[str, Any]:
        engine = self.engine

        engine_version: Union[None, str]
        engine_version = self.engine_version

        metrics_format: Union[None, str]
        if isinstance(self.metrics_format, str):
            metrics_format = self.metrics_format
        else:
            metrics_format = self.metrics_format

        metrics_path: Union[None, str]
        metrics_path = self.metrics_path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "engine": engine,
            "engine_version": engine_version,
            "metrics_format": metrics_format,
            "metrics_path": metrics_path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        engine = d.pop("engine")

        def _parse_engine_version(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        engine_version = _parse_engine_version(d.pop("engine_version"))


        def _parse_metrics_format(data: object) -> Union[CompiledRuntimeTelemetryMetricsFormatType0, None]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                metrics_format_type_0 = check_compiled_runtime_telemetry_metrics_format_type_0(data)



                return metrics_format_type_0
            except: # noqa: E722
                pass
            return cast(Union[CompiledRuntimeTelemetryMetricsFormatType0, None], data)

        metrics_format = _parse_metrics_format(d.pop("metrics_format"))


        def _parse_metrics_path(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        metrics_path = _parse_metrics_path(d.pop("metrics_path"))


        compiled_runtime_telemetry = cls(
            engine=engine,
            engine_version=engine_version,
            metrics_format=metrics_format,
            metrics_path=metrics_path,
        )

        return compiled_runtime_telemetry
