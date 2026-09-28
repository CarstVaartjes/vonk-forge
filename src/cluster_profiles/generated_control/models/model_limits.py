from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ModelLimits")



@_attrs_define
class ModelLimits:
    """
        Attributes:
            context_tokens (int | None | Unset):
            frames (int | None | Unset):
            resolution_pixels (int | None | Unset):
            sample_rate_hz (int | None | Unset):
     """

    context_tokens: int | None | Unset = UNSET
    frames: int | None | Unset = UNSET
    resolution_pixels: int | None | Unset = UNSET
    sample_rate_hz: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        context_tokens: int | None | Unset
        if isinstance(self.context_tokens, Unset):
            context_tokens = UNSET
        else:
            context_tokens = self.context_tokens

        frames: int | None | Unset
        if isinstance(self.frames, Unset):
            frames = UNSET
        else:
            frames = self.frames

        resolution_pixels: int | None | Unset
        if isinstance(self.resolution_pixels, Unset):
            resolution_pixels = UNSET
        else:
            resolution_pixels = self.resolution_pixels

        sample_rate_hz: int | None | Unset
        if isinstance(self.sample_rate_hz, Unset):
            sample_rate_hz = UNSET
        else:
            sample_rate_hz = self.sample_rate_hz


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if context_tokens is not UNSET:
            field_dict["context_tokens"] = context_tokens
        if frames is not UNSET:
            field_dict["frames"] = frames
        if resolution_pixels is not UNSET:
            field_dict["resolution_pixels"] = resolution_pixels
        if sample_rate_hz is not UNSET:
            field_dict["sample_rate_hz"] = sample_rate_hz

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_context_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        context_tokens = _parse_context_tokens(d.pop("context_tokens", UNSET))


        def _parse_frames(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        frames = _parse_frames(d.pop("frames", UNSET))


        def _parse_resolution_pixels(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        resolution_pixels = _parse_resolution_pixels(d.pop("resolution_pixels", UNSET))


        def _parse_sample_rate_hz(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sample_rate_hz = _parse_sample_rate_hz(d.pop("sample_rate_hz", UNSET))


        model_limits = cls(
            context_tokens=context_tokens,
            frames=frames,
            resolution_pixels=resolution_pixels,
            sample_rate_hz=sample_rate_hz,
        )

        return model_limits
