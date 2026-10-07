from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.model_cache_cancellation_observation import ModelCacheCancellationObservation





T = TypeVar("T", bound="ModelCacheCancellation")



@_attrs_define
class ModelCacheCancellation:
    """ Durable record of the one accepted cancellation request.

        Attributes:
            actor (str):
            reason (str):
            request_key (str):
            requested_at (str):
            observation (ModelCacheCancellationObservation | None | Unset):
     """

    actor: str
    reason: str
    request_key: str
    requested_at: str
    observation: ModelCacheCancellationObservation | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_cancellation_observation import ModelCacheCancellationObservation # noqa: PLC0415
        actor = self.actor

        reason = self.reason

        request_key = self.request_key

        requested_at = self.requested_at

        observation: dict[str, Any] | None | Unset
        if isinstance(self.observation, Unset):
            observation = UNSET
        elif isinstance(self.observation, ModelCacheCancellationObservation):
            observation = self.observation.to_dict()
        else:
            observation = self.observation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "reason": reason,
            "request_key": request_key,
            "requested_at": requested_at,
        })
        if observation is not UNSET:
            field_dict["observation"] = observation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_cache_cancellation_observation import ModelCacheCancellationObservation # noqa: PLC0415
        d = dict(src_dict)
        actor = d.pop("actor")

        reason = d.pop("reason")

        request_key = d.pop("request_key")

        requested_at = d.pop("requested_at")

        def _parse_observation(data: object) -> ModelCacheCancellationObservation | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                observation_type_0 = ModelCacheCancellationObservation.from_dict(data)



                return observation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheCancellationObservation | None | Unset, data)

        observation = _parse_observation(d.pop("observation", UNSET))


        model_cache_cancellation = cls(
            actor=actor,
            reason=reason,
            request_key=request_key,
            requested_at=requested_at,
            observation=observation,
        )

        return model_cache_cancellation
