"""Secret-free projection of one published recipe endpoint."""

from __future__ import annotations

import re

from pydantic import ConfigDict, Field

from .strict_json import StrictJSONModel

_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


# One DNS name with an optional port, exactly as a client's Host header names
# the Controller origin it reached.
_GATEWAY_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?::[1-9][0-9]{0,4})?"
)


def inference_gateway_api_base(host: str | None) -> str:
    """The client-facing OpenAI base URL for the Controller origin a client used.

    Inference is served only through the Controller's own HTTPS origin: Caddy
    routes `/v1/*` to LiteLLM behind the route-lease check, on the Tailscale
    service and on the Lab listener alike.  A Spark's node address is a
    backend detail that clients generally cannot reach, so it is never the
    primary endpoint.  Caddy forwards only the canonical control hostname
    here, so the request's Host names exactly that origin.
    """

    value = (host or "").strip().lower()
    if len(value) > 253 or _GATEWAY_HOST.fullmatch(value) is None:
        raise ValueError("controller origin host is invalid")
    return f"https://{value}/v1"


class EndpointResponse(StrictJSONModel):
    """One published alias: clients use `api_base` with `alias` as the model.

    `backend_api_base` is the Spark-local serving address LiteLLM routes to.
    It is diagnostic only and usually unreachable from a client.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    alias: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=63)
    api_base: str = Field(min_length=1, max_length=512)
    backend_api_base: str = Field(min_length=1, max_length=512)
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=_NODE_PATTERN)
    observed_at: str = Field(min_length=1, max_length=64)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
