"""Original requests, distinct from resolved preparation and worker effects."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from .strict_json import StrictJSONModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RecipeBuildDependency(StrictJSONModel):
    """A request persisted before dispatch, then its exact lifecycle child."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: UUID
    operation_id: UUID | None = None


class RecipeSelectorIntent(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["selector"] = "selector"
    selector: str = Field(min_length=1, max_length=256)
    force: bool = False


class RecipeRevisionIntent(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["revision"] = "revision"
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    model_digest: Digest | None = None
    build_input_sha256: Digest | None = None
    effective_execution_key: Digest | None = None
    force: bool = False
    force_download: bool = False
    force_rebuild: bool = False

    @model_validator(mode="after")
    def one_image_action(self) -> RecipeRevisionIntent:
        if sum((self.force, self.force_download, self.force_rebuild)) > 1:
            raise ValueError("image actions are mutually exclusive")
        return self


class RecipeRetryIntent(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["retry"] = "retry"
    operation_id: str = Field(min_length=1, max_length=128)


RecipeAvailabilityIntent = Annotated[
    RecipeSelectorIntent | RecipeRevisionIntent | RecipeRetryIntent,
    Field(discriminator="kind"),
]
_INTENT = TypeAdapter(RecipeAvailabilityIntent)


def read_availability_intent(value: object) -> RecipeAvailabilityIntent:
    return _INTENT.validate_json(json.dumps(value, allow_nan=False))
