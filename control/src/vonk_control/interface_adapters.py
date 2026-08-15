"""Engine-independent interface behavior and publication authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class InterfaceAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InterfaceAdapter:
    name: str
    publication: str

    def readiness(self, status: Mapping[str, object]) -> bool:
        return status.get("ready") is True

    def invocation_request(self, request: Mapping[str, object]) -> dict[str, object]:
        return dict(request)

    def evidence(self, result: Mapping[str, object]) -> dict[str, object]:
        return dict(result)

    def withdrawal(self) -> dict[str, object]:
        return {"publication": self.publication, "withdrawn": True}


_ADAPTERS = {
    "openai": InterfaceAdapter("openai", "litellm"),
    "image-job": InterfaceAdapter("image-job", "artifact"),
    "audio-job": InterfaceAdapter("audio-job", "artifact"),
    "video-job": InterfaceAdapter("video-job", "artifact"),
    "mesh-job": InterfaceAdapter("mesh-job", "artifact"),
    "artifact-job": InterfaceAdapter("artifact-job", "artifact"),
}


def interface_adapter(name: str) -> InterfaceAdapter:
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise InterfaceAdapterError("unknown interface adapter")
    return adapter
