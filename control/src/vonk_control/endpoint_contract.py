"""Secret-free projection of one published recipe endpoint."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from .strict_json import StrictJSONModel

_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class EndpointResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    alias: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=63)
    api_base: str = Field(min_length=1, max_length=512)
    expires_at: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=_NODE_PATTERN)
    observed_at: str = Field(min_length=1, max_length=64)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    state: str = Field(pattern=r"^published$")
