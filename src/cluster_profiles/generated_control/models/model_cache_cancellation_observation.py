from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_cancellation_observation_effect import check_model_cache_cancellation_observation_effect
from ..models.model_cache_cancellation_observation_effect import ModelCacheCancellationObservationEffect
from typing import cast






T = TypeVar("T", bound="ModelCacheCancellationObservation")



@_attrs_define
class ModelCacheCancellationObservation:
    """ The lifecycle owner's actual terminal cancellation evidence.

        Attributes:
            detail (str):
            effect (ModelCacheCancellationObservationEffect):
            observed_at (str):
     """

    detail: str
    effect: ModelCacheCancellationObservationEffect
    observed_at: str





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        effect: str = self.effect

        observed_at = self.observed_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "effect": effect,
            "observed_at": observed_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        effect = check_model_cache_cancellation_observation_effect(d.pop("effect"))




        observed_at = d.pop("observed_at")

        model_cache_cancellation_observation = cls(
            detail=detail,
            effect=effect,
            observed_at=observed_at,
        )

        return model_cache_cancellation_observation
