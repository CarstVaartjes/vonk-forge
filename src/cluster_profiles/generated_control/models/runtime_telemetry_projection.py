from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RuntimeTelemetryProjection")



@_attrs_define
class RuntimeTelemetryProjection:
    """
        Attributes:
            engine (str):
            engine_version (None | str | Unset):
            metrics_format (None | str | Unset):
            metrics_path (None | str | Unset):
     """

    engine: str
    engine_version: None | str | Unset = UNSET
    metrics_format: None | str | Unset = UNSET
    metrics_path: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        engine = self.engine

        engine_version: None | str | Unset
        if isinstance(self.engine_version, Unset):
            engine_version = UNSET
        else:
            engine_version = self.engine_version

        metrics_format: None | str | Unset
        if isinstance(self.metrics_format, Unset):
            metrics_format = UNSET
        else:
            metrics_format = self.metrics_format

        metrics_path: None | str | Unset
        if isinstance(self.metrics_path, Unset):
            metrics_path = UNSET
        else:
            metrics_path = self.metrics_path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "engine": engine,
        })
        if engine_version is not UNSET:
            field_dict["engine_version"] = engine_version
        if metrics_format is not UNSET:
            field_dict["metrics_format"] = metrics_format
        if metrics_path is not UNSET:
            field_dict["metrics_path"] = metrics_path

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        engine = d.pop("engine")

        def _parse_engine_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        engine_version = _parse_engine_version(d.pop("engine_version", UNSET))


        def _parse_metrics_format(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        metrics_format = _parse_metrics_format(d.pop("metrics_format", UNSET))


        def _parse_metrics_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        metrics_path = _parse_metrics_path(d.pop("metrics_path", UNSET))


        runtime_telemetry_projection = cls(
            engine=engine,
            engine_version=engine_version,
            metrics_format=metrics_format,
            metrics_path=metrics_path,
        )

        return runtime_telemetry_projection
