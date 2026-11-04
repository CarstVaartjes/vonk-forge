"""The one rule for a model alias: the name a client sends as the model.

A profile names each assignment's alias; the run plan, the route bundle, the
LiteLLM model list, the endpoint discovery API and gateway key grants all carry
that same name. Every one of them uses this type, so a name the profile accepts
is never refused further down the path.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

ALIAS_MAX_LENGTH = 128
ALIAS_PATTERN = rf"^[a-z0-9](?:[a-z0-9._-]{{0,{ALIAS_MAX_LENGTH - 2}}}[a-z0-9])?$"

Alias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=ALIAS_MAX_LENGTH, pattern=ALIAS_PATTERN),
]
