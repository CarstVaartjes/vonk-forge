"""An index block read without the words this Controller does not know.

The catalog reader keeps a word a newer contract minor added as an
``UnrecognisedWord``. The Controller never stores or serves one (an older
vonkctl reads these fields as closed enums), but one unknown word must not cost
the rest of its block either. So :func:`known_block` rebuilds the block:

- a word in a list (an endpoint, a format, a capability, a change) is left out;
- a scalar word becomes the neutral value :data:`NEUTRAL` names for its field,
  one that fails toward caution (an unknown published status reads "Not yet
  verified", never Working);
- a scalar word with no neutral value leaves out the nearest enclosing object
  that may be left out: an optional or nullable field (an evidence record, an
  effort block) or a list item (an option choice);
- if nothing encloses it but the block itself, or the block is invalid
  without it, :class:`BlockWithheld` is raised and the caller withholds the
  block and notes it.

The rebuilt block is validated by the contract without the reader's context,
which refuses any unknown word, so what comes back holds known words only.
"""

from __future__ import annotations

import json
import types
from typing import Final, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from vonk_forge_contracts import RecipeDisplay, UnrecognisedWord
from vonk_forge_contracts.api import ApiReasoningSwitch
from vonk_forge_contracts.qualification_status import (
    PublishedQualificationStatusView,
    QualificationPlatform,
    QualificationStatus,
)

# JSON as the contract dumps it; what a rebuilt block is validated from.
type _Json = str | int | float | bool | None | list[_Json] | dict[str, _Json]

#: A scalar field's neutral value for a word this Controller does not know.
#: A scalar word in any other field leaves out its enclosing object.
NEUTRAL: Final[dict[tuple[type[BaseModel], str], _Json]] = {
    (PublishedQualificationStatusView, "status"): QualificationStatus.NOT_YET_VERIFIED,
    # There is no generic reason; no reason is the neutral one.
    (PublishedQualificationStatusView, "reason"): None,
    # Not recorded, rather than a topology this release cannot name.
    (QualificationPlatform, "topology"): None,
    # The model's own default, rather than forcing thinking on or off.
    (ApiReasoningSwitch, "default"): "model",
    (RecipeDisplay, "alignment"): "unspecified",
}


class _NoNeutral(Exception):
    """A scalar word with no neutral value: leave out its enclosing object."""


class BlockWithheld(Exception):
    """The block cannot stand without its unknown words."""


def known_block[B: BaseModel](block: B) -> B:
    """``block`` without unknown words (raises :class:`BlockWithheld`)."""

    try:
        rebuilt = _model(block, block.model_dump(mode="json"))
        return type(block).model_validate_json(json.dumps(rebuilt))
    except (_NoNeutral, ValidationError) as error:
        raise BlockWithheld from error


def _model(model: BaseModel, dumped: dict[str, _Json]) -> dict[str, _Json]:
    rebuilt = dict(dumped)
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if isinstance(value, UnrecognisedWord):
            key = (type(model), name)
            if key not in NEUTRAL:
                raise _NoNeutral
            rebuilt[name] = NEUTRAL[key]
            continue
        try:
            rebuilt[name] = _value(value, dumped[name])
        except _NoNeutral:
            if not _nullable(field.annotation):
                raise
            rebuilt[name] = None
    return rebuilt


def _value(value: object, dumped: _Json) -> _Json:
    if isinstance(value, BaseModel) and isinstance(dumped, dict):
        return _model(value, dumped)
    if isinstance(value, list | tuple) and isinstance(dumped, list):
        kept: list[_Json] = []
        for item, item_dumped in zip(value, dumped, strict=True):
            if isinstance(item, UnrecognisedWord):
                continue
            try:
                kept.append(_value(item, item_dumped))
            except _NoNeutral:
                continue
        return kept
    return dumped


def _nullable(annotation: object) -> bool:
    if annotation is None or annotation is types.NoneType:
        return True
    if get_origin(annotation) in (Union, types.UnionType):
        return any(_nullable(arg) for arg in get_args(annotation))
    return False
