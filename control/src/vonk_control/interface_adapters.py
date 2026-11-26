"""Engine-independent interface behavior and publication authority."""

from __future__ import annotations

from dataclasses import dataclass


class InterfaceAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InterfaceAdapter:
    name: str
    publication: str


# The interface adapters this platform executes; scripts/recipes/platform-features
# publishes these keys to recipes CI.
INTERFACE_ADAPTERS: dict[str, InterfaceAdapter] = {
    "openai": InterfaceAdapter("openai", "litellm"),
    "image-job": InterfaceAdapter("image-job", "artifact"),
    "audio-job": InterfaceAdapter("audio-job", "artifact"),
    "video-job": InterfaceAdapter("video-job", "artifact"),
    "mesh-job": InterfaceAdapter("mesh-job", "artifact"),
    "artifact-job": InterfaceAdapter("artifact-job", "artifact"),
}


def interface_adapter(name: str) -> InterfaceAdapter:
    if type(name) is not str:
        raise InterfaceAdapterError("unknown interface adapter")
    adapter = INTERFACE_ADAPTERS.get(name)
    if adapter is None:
        raise InterfaceAdapterError("unknown interface adapter")
    return adapter
