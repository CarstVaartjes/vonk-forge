"""Recipe update batches: helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..recipe_image_availability import (
    RecipeImageAvailabilityUnknown,
)
from ..recipe_update_contract import (
    RecipeUpdateBinding,
    RecipeUpdateDocument,
)
from ..strict_json import serialize_json_value

_SETTLED = frozenset({"succeeded", "failed", "cancelled"})


_OBSERVATION_INTERVAL = timedelta(seconds=2)


@dataclass(frozen=True)
class RecipeUpdateClaim:
    operation_id: str
    owner: str


class RecipeUpdateClaimLost(RecipeImageAvailabilityUnknown):
    """This invocation ended; a newer owner or cancellation now controls the batch."""


def _encoded(document: object) -> bytes:
    return json.dumps(
        serialize_json_value(document),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _binding_digest(document: RecipeUpdateDocument) -> str:
    binding = RecipeUpdateBinding(
        request=document.request,
        children=[child.identity() for child in document.children],
    )
    return hashlib.sha256(_encoded(binding)).hexdigest()


def _now(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
