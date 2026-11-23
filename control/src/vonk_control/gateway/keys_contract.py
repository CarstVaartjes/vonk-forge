"""Wire documents of gateway client keys."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from ..strict_json import StrictJSONModel


class GatewayKeyView(StrictJSONModel):
    """One gateway client key without its secret. Empty `models` means all."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=256)
    models: list[str]
    created_at: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None


class GatewayKeyCreated(GatewayKeyView):
    """The retained key for this mutation; the exact receipt can be replayed."""

    key: str = Field(min_length=1, max_length=512)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class GatewayKeyRevoked(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=63)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
