"""Print what the pinned LiteLLM accepts: the source of ``surface.json``.

Run it inside the pinned image so it reads the real classes; it builds nothing::

    docker run --rm -i -e VONK_LITELLM_IMAGE_DIGEST=sha256:... \
        --entrypoint python <litellm image> - < tools/litellm_surface.py

The output is deterministic (sorted keys, one trailing newline), so CI compares it
byte for byte with ``deploy/compose/litellm/surface.json``.
"""

from __future__ import annotations

import json
import os
from importlib import import_module
from importlib.metadata import version
from typing import get_type_hints

from pydantic import TypeAdapter

# litellm exists only in the pinned image, so it is imported by name.
LiteLLM_Params = import_module("litellm.types.router").LiteLLM_Params
LlmProviders = import_module("litellm.types.utils").LlmProviders
ModelInfoFields = import_module("litellm.types.utils").ModelInfo

# A type JSON cannot carry (a callable, a client object) is refused outright.
_NOT_JSON = {"not": {}}


def _schema(annotation: object) -> object:
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:  # noqa: BLE001 -- any type pydantic cannot describe is not JSON
        return _NOT_JSON


def surface() -> object:
    params = {
        name: _schema(field.annotation)
        for name, field in LiteLLM_Params.model_fields.items()
    }
    info = {
        name: _schema(annotation)
        for name, annotation in get_type_hints(ModelInfoFields).items()
    }
    return {
        "image_digest": os.environ["VONK_LITELLM_IMAGE_DIGEST"],
        "litellm_params": params,
        "litellm_version": version("litellm"),
        "model_info": info,
        "providers": sorted(provider.value for provider in LlmProviders),
    }


print(json.dumps(surface(), sort_keys=True, indent=1))
