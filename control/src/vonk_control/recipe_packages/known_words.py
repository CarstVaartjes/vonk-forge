"""An index block read without the words this Controller does not know.

The catalog reader keeps a word a newer contract minor added as an
``UnrecognisedWord``. A capability is shown by its name: the release's own
vocabulary labels it, and the Library serves capabilities as names. So a
capability word that fits a served name, at most 64 characters (:data:`KEPT`,
and an entry's ``served_capabilities``), is kept. No other unknown word is stored or served,
but one must not cost the rest of its block either. So :func:`known_block`
rebuilds the block:

- a word in any other list (an endpoint, a format, a modality, a change) is
  left out;
- a scalar word becomes the neutral value :data:`NEUTRAL` names for its field,
  one that fails toward caution (an unknown published status reads "Not yet
  verified", never Working);
- a scalar word with no neutral value leaves out the nearest enclosing object
  that may be left out: an optional or nullable field (an evidence record, an
  effort block) or a list item (an option choice);
- if nothing encloses it but the block itself, or the block is invalid
  without it, :class:`BlockWithheld` is raised and the caller withholds the
  block and notes it.

The rebuilt block is validated by the contract, in the reader's context only
when it keeps a capability name, so what comes back holds known words plus the
kept capability names only. Any failure to rebuild one block, expected or not,
withholds that block alone.
"""

from __future__ import annotations

import json
import types
from typing import Final, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError
from vonk_forge_contracts import ModelDisplay, RecipeDisplay, UnrecognisedWord
from vonk_forge_contracts.api import ApiReasoningSwitch
from vonk_forge_contracts.qualification_status import (
    PublishedQualificationStatusView,
    QualificationPlatform,
    QualificationStatus,
)
from vonk_forge_contracts.unrecognised import read_context

from vonk_control.library.contract import CapabilityName

# JSON as the contract dumps it; what a rebuilt block is validated from.
type _Json = str | int | float | bool | list[_Json] | dict[str, _Json] | None

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


#: The capability lists whose unknown words are kept as names.
KEPT: Final[frozenset[tuple[type[BaseModel], str]]] = frozenset(
    {(ModelDisplay, "capabilities")}
)
#: What a kept word must be: the name the Library serves.
_CAPABILITY_NAME: Final = TypeAdapter(CapabilityName)


class _NoNeutral(Exception):
    """A scalar word with no neutral value: leave out its enclosing object."""


#: Why a block is withheld when it cannot stand without its unknown words.
WITHOUT_UNKNOWN_WORD: Final = "it cannot stand without an unknown word"


class BlockWithheld(Exception):
    """The block is withheld; ``reason`` says why, for the sync's note."""

    def __init__(self, reason: str = WITHOUT_UNKNOWN_WORD) -> None:
        super().__init__(reason)
        self.reason = reason


def known_block[B: BaseModel](block: B) -> B:
    """``block`` without unknown words (raises :class:`BlockWithheld`)."""

    try:
        rebuilt = _model(block, block.model_dump(mode="json"))
        # Only a block that keeps a capability name validates in the reader's
        # context; any other refuses an unknown word that is left.
        keeps = any(type(block) is model for model, _ in KEPT)
        return type(block).model_validate_json(
            json.dumps(rebuilt), context=read_context() if keeps else None
        )
    except (_NoNeutral, ValidationError) as error:
        raise BlockWithheld from error
    # Any other failure costs only this block too, and its note names it.
    except Exception as error:
        raise BlockWithheld(f"its rebuild failed ({type(error).__name__})") from error


def known_field[V: BaseModel | tuple[str, ...]](value: V) -> V:
    """A block, or an entry's capability list (``served_capabilities``), read
    without the unknown words it cannot keep."""

    if isinstance(value, BaseModel):
        return known_block(value)
    if not isinstance(value, tuple | list):  # pyright: ignore[reportUnnecessaryIsInstance]  # a field the types do not promise: withhold it, never split it
        raise BlockWithheld
    return tuple(  # pyright: ignore[reportReturnType]  # the same tuple type, fewer items
        item for item in value if _kept_capability(item)
    )


def _kept_capability(word: object) -> bool:
    """A known capability, or a newer one that fits a served name."""

    if not isinstance(word, UnrecognisedWord):
        return True
    try:
        _CAPABILITY_NAME.validate_python(str(word))
    except ValidationError:
        return False
    return True


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
        field_dumped = dumped[name]
        if (
            (type(model), name) in KEPT
            and isinstance(value, list | tuple)
            and isinstance(field_dumped, list)
        ):
            rebuilt[name] = [
                item_dumped
                for item, item_dumped in zip(value, field_dumped, strict=True)
                if _kept_capability(item)
            ]
            continue
        try:
            rebuilt[name] = _value(value, field_dumped)
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
