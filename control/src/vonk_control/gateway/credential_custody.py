"""What the gateway supervisor says it loaded: the Controller's only view of the secrets.

The secret values stay in NAS custody (``secrets/gateway-credentials/<BINDING>``),
are staged by ``runtime-init`` for LiteLLM alone, and are exported by the supervisor
into the LiteLLM child's environment as ``VONK_CREDENTIAL_<BINDING>``.  After the
child is healthy the supervisor writes ``/supervisor/credentials.json`` with the
binding names it loaded and the child's pid.  The document never carries a value.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from vonk_agent_protocol.lifecycle_vocabulary import ErrorCategory, WaitReason
from vonk_agent_protocol.outcome import UnknownError
from vonk_control.gateway.external_contract import BindingName
from vonk_control.strict_json import StrictModel

#: Where the supervisor writes the loaded names (the ``litellm-supervisor-state`` volume).
CREDENTIALS_PATH: Final = Path("/supervisor/credentials.json")
#: The staged secret files LiteLLM's supervisor reads, one per binding.
STAGED_CREDENTIALS_DIRECTORY: Final = "gateway-credentials"
#: The bound on one secret file (an inline service-account document is the largest).
MAX_CREDENTIAL_BYTES: Final = 16 * 1024
_MAX_DOCUMENT_BYTES: Final = 64 * 1024


class LoadedCredentials(StrictModel):
    """``credentials.json``: the binding names one LiteLLM child was started with."""

    schema_version: Literal[1] = 1
    child_pid: int = Field(ge=1)
    names: list[BindingName]


def read_loaded_credentials(
    path: Path = CREDENTIALS_PATH,
) -> LoadedCredentials | UnknownError:
    """The supervisor's document, or an unknown outcome when it has not said (a wait)."""

    unsaid = UnknownError(
        category=ErrorCategory.UNKNOWN, reason=WaitReason.OBSERVATION_UNAVAILABLE
    )
    try:
        if path.is_symlink() or not path.is_file():
            return unsaid
        content = path.read_bytes()
    except OSError:
        return unsaid
    if len(content) > _MAX_DOCUMENT_BYTES:
        return unsaid
    try:
        return LoadedCredentials.model_validate_json(content)
    except ValidationError:
        return unsaid


def loaded_names(
    read: Callable[[], LoadedCredentials | UnknownError] = read_loaded_credentials,
) -> frozenset[str] | None:
    """The names the running child holds, or ``None`` when the supervisor has not said."""

    loaded = read()
    return frozenset(loaded.names) if isinstance(loaded, LoadedCredentials) else None


__all__ = [
    "CREDENTIALS_PATH",
    "MAX_CREDENTIAL_BYTES",
    "STAGED_CREDENTIALS_DIRECTORY",
    "LoadedCredentials",
    "loaded_names",
    "read_loaded_credentials",
]
