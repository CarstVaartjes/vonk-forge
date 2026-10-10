from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.prometheus_attention_labels import PrometheusAttentionLabels





T = TypeVar("T", bound="PrometheusAttention")



@_attrs_define
class PrometheusAttention:
    """
        Attributes:
            active_at (str):
            labels (PrometheusAttentionLabels):
            name (str):
            severity (str):
            summary (str):
            source (Literal['prometheus'] | Unset):  Default: 'prometheus'.
     """

    active_at: str
    labels: PrometheusAttentionLabels
    name: str
    severity: str
    summary: str
    source: Literal['prometheus'] | Unset = 'prometheus'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.prometheus_attention_labels import PrometheusAttentionLabels # noqa: PLC0415
        active_at = self.active_at

        labels = self.labels.to_dict()

        name = self.name

        severity = self.severity

        summary = self.summary

        source = self.source


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "active_at": active_at,
            "labels": labels,
            "name": name,
            "severity": severity,
            "summary": summary,
        })
        if source is not UNSET:
            field_dict["source"] = source

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.prometheus_attention_labels import PrometheusAttentionLabels # noqa: PLC0415
        d = dict(src_dict)
        active_at = d.pop("active_at")

        labels = PrometheusAttentionLabels.from_dict(d.pop("labels"))




        name = d.pop("name")

        severity = d.pop("severity")

        summary = d.pop("summary")

        source = cast(Literal['prometheus'] | Unset , d.pop("source", UNSET))
        if source != 'prometheus' and not isinstance(source, Unset):
            raise ValueError(f"source must match const 'prometheus', got '{source}'")

        prometheus_attention = cls(
            active_at=active_at,
            labels=labels,
            name=name,
            severity=severity,
            summary=summary,
            source=source,
        )


        prometheus_attention.additional_properties = d
        return prometheus_attention

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
