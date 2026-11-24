"""Constrained values and the base model the recipe-job wire models share."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import StringConstraints, ValidationError

from vonk_agent_protocol.wire_model import AgentProtocolError, WireModel

_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SLOT = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
MEDIA_TYPE_PATTERN = (
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)

ArtifactName = Annotated[str, StringConstraints(pattern=_NAME)]
ArtifactSlot = Annotated[str, StringConstraints(pattern=_SLOT)]
MediaType = Annotated[str, StringConstraints(pattern=MEDIA_TYPE_PATTERN)]


def _thaw(value: object) -> object:
    """Turn the immutable envelope projection back into Pydantic input."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


class _RecipeJobModel(WireModel):
    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _parse_model[ModelT: _RecipeJobModel](
    cls: type[ModelT], raw: Any, *, label: str
) -> ModelT:
    try:
        return cls.model_validate(_thaw(raw))
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(item) for item in first.get("loc", ()))
        message = str(first.get("msg", f"{label} is invalid"))
        raise AgentProtocolError(
            f"{location}: {message}" if location else message
        ) from error
