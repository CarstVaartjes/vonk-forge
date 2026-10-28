"""Optional agent evidence never fails the mandatory part of a report.

An agent report has a mandatory core (identity, lease, capacity, the outcome of
an operation) and optional evidence (NICs, the NAS route, fabric details,
sensor readings, progress, failure diagnostics). Refusing the whole report for
a damaged optional part loses the core: an inventory stops updating, a result
is lost with its lease. A model that mixes in :class:`OptionalEvidenceModel`
names its optional evidence as groups of field paths; when validation fails and
dropping some groups makes it pass, the groups are dropped and the model carries
one typed :class:`EvidenceDrop` per dropped group: its code and the field that
refused it, so a stored report can say what it lost. When the core itself is
invalid the original error is raised unchanged.

This is confined to evidence the Controller only displays or ignores. Identity,
time, fences, fabric *policy* decisions, versions and every other mandatory
field stay strict, and nothing is repaired into a different valid value: a
damaged optional part is removed, never rewritten.

One rewrite is allowed, in prose only: a model names its free-text leaves
(``HOST_PATH_TEXT``: a reason, a summary, a one-line diagnostic, at any
depth). A host path in one of them is replaced by the typed marker
``<host-path>`` and the report carries ``RESULT_HOST_PATH_REDACTED``. The rest
of the sentence is kept, and the report's core is never refused for it.
"""

from __future__ import annotations

import copy
import itertools
import re
from collections.abc import Mapping
from typing import Any, ClassVar, NamedTuple

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator
from pydantic_core.core_schema import ValidatorFunctionWrapHandler

from .reason_codes import AgentEvidenceCode
from .wire_model import WireModel


class EvidenceGroup(NamedTuple):
    """Optional fields that are valid together or dropped together."""

    code: AgentEvidenceCode
    #: Each path is the keys from the model's input down to one optional field.
    paths: tuple[tuple[str, ...], ...]


#: One key of a refused field's location: a model key, or ``*`` for any other
#: (a free-form mapping key), so a location is never path- or secret-bearing.
_LOCATION_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class EvidenceDrop(WireModel):
    """One optional evidence group a report lost, and the field that refused it."""

    code: AgentEvidenceCode
    #: The refused field inside the group: keys from the report's root, by ``.``.
    location: str = Field(pattern=r"^[A-Za-z0-9_*.-]{1,256}$")
    #: Pydantic's error type for that field, such as ``is_instance_of``.
    error_type: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


#: What stands in for a host path inside prose. It carries no path separator.
HOST_PATH_MARKER = "<host-path>"
# A path is a token with a separator in it; the whole token is replaced, so no
# separator survives in the sentence. ``key=`` before it and ``:line`` after it
# are not part of the token and are kept.
_PATH_TOKEN = re.compile(r"[^\s'\"`()\[\]{},;=:]*[/\\][^\s'\"`()\[\]{},;=:]*")


# The shortest bound of any prose leaf (``recovery``): a redaction that makes a
# sentence longer is cut to this or its own length, so it stays within the
# bound the original met.
_SHORTEST_PROSE_BOUND = 128


def redact_host_paths(text: str) -> str:
    """``text`` with every path-like token replaced by :data:`HOST_PATH_MARKER`.

    The agent applies the same rule before sending (shared vector
    ``host-path-redaction-v1.json``).
    """

    redacted = _PATH_TOKEN.sub(HOST_PATH_MARKER, text)
    return redacted[: max(len(text), _SHORTEST_PROSE_BOUND)]


def redacted_prose(document: object, names: frozenset[str]) -> object:
    """``document`` with host paths redacted in its prose leaves, or ``None`` if none had one.

    A prose leaf is a string under one of ``names`` at any depth, so a reason
    nested in a receipt, an ending or a finding is covered like a top-level one.
    """

    if not isinstance(document, Mapping):
        return None
    redacted = copy.deepcopy(dict(document))
    changed = False
    pending: list[object] = [redacted]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in names and isinstance(value, str):
                    if "/" in value or "\\" in value:
                        node[key] = redact_host_paths(value)
                        changed = True
                else:
                    pending.append(value)
    return redacted if changed else None


def _present(document: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False
        node = node[key]
    return True


def _without(
    document: Mapping[str, Any], groups: tuple[EvidenceGroup, ...]
) -> dict[str, Any]:
    stripped = copy.deepcopy(dict(document))
    for group in groups:
        for path in group.paths:
            node: Any = stripped
            for key in path[:-1]:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(path[-1], None)
    return stripped


def _data_location(data: Any, loc: tuple[int | str, ...]) -> tuple[str, ...]:
    """The keys of ``loc`` that address ``data``; a union member's tag is skipped."""

    keys: list[str] = []
    node: Any = data
    for part in loc:
        if isinstance(node, Mapping) and isinstance(part, str) and part in node:
            keys.append(part if _LOCATION_KEY.match(part) else "*")
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            keys.append(str(part))
            node = node[part]
    return tuple(keys)


def _error_type(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())[:64] or "value_error"


def _drop(group: EvidenceGroup, data: Any, refusal: ValidationError) -> EvidenceDrop:
    """``group``'s drop, located at the field inside it that ``refusal`` names.

    A union reports every member's refusal; the member that reads the group
    reaches deepest into it, so the deepest location is the field at fault.
    """

    present = [path for path in group.paths if _present(data, path)]
    errors = refusal.errors()
    located = [
        (keys, error)
        for error in errors
        if any(
            (keys := _data_location(data, tuple(error["loc"])))[: len(path)] == path
            for path in present
        )
    ]
    if located:
        keys, error = max(located, key=lambda item: len(item[0]))
        return EvidenceDrop(
            code=group.code,
            location=".".join(keys)[:256],
            error_type=_error_type(error["type"]),
        )
    # Refused by a check of the whole report rather than by one of its fields.
    return EvidenceDrop(
        code=group.code,
        location=".".join(present[0])[:256],
        error_type=_error_type(errors[0]["type"] if errors else "value_error"),
    )


class OptionalEvidenceModel(BaseModel):
    """Mixin: drop invalid optional evidence instead of refusing the report."""

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = ()
    #: Free-text leaves (paths from the model's input) whose host paths are
    #: redacted rather than refusing the report.
    HOST_PATH_TEXT: ClassVar[frozenset[str]] = frozenset()
    _evidence_warnings: tuple[AgentEvidenceCode, ...] = PrivateAttr(default=())
    _evidence_drops: tuple[EvidenceDrop, ...] = PrivateAttr(default=())

    @property
    def evidence_warnings(self) -> tuple[AgentEvidenceCode, ...]:
        """The optional evidence this report lost, empty when it arrived whole."""

        return self._evidence_warnings

    @property
    def evidence_drops(self) -> tuple[EvidenceDrop, ...]:
        """Each dropped group with the field that refused it; empty when none was."""

        return self._evidence_drops

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        validators = list(cls.__pydantic_decorators__.model_validators)
        if cls.EVIDENCE_GROUPS and (
            not validators or validators[-1] != _VALIDATOR_NAME
        ):
            raise TypeError(
                f"{cls.__name__} declares optional evidence: end its body with "
                f"`{_VALIDATOR_NAME} = fail_open_on_optional_evidence()` so the "
                "validator runs outside the model's own consistency checks"
            )


_VALIDATOR_NAME = "drop_invalid_optional_evidence"


def _drop_invalid_optional_evidence(
    cls: type[OptionalEvidenceModel], data: Any, handler: ValidatorFunctionWrapHandler
) -> Any:
    try:
        return handler(data)
    except ValidationError as error:
        if not isinstance(data, Mapping):
            raise
        refusal = error
        redacted: tuple[AgentEvidenceCode, ...] = ()
        prose = redacted_prose(data, cls.HOST_PATH_TEXT)
        if prose is not None:
            # A host path in prose is redacted, never a reason to refuse.
            data, redacted = prose, (AgentEvidenceCode.RESULT_HOST_PATH_REDACTED,)
            try:
                model = handler(data)
            except ValidationError as still:
                refusal = still
            else:
                model._evidence_warnings = redacted
                return model
        present = tuple(
            group
            for group in cls.EVIDENCE_GROUPS
            if any(_present(data, path) for path in group.paths)
        )
        # Fewest groups first: lose as little as the core allows.
        for size in range(1, len(present) + 1):
            for dropped in itertools.combinations(present, size):
                try:
                    model = handler(_without(data, dropped))
                except ValidationError:
                    continue
                model._evidence_warnings = redacted + tuple(
                    group.code for group in dropped
                )
                model._evidence_drops = tuple(
                    _drop(group, data, refusal) for group in dropped
                )
                return model
        raise


def fail_open_on_optional_evidence() -> Any:
    """The wrap validator to assign, last, in the body of a model with groups.

    Pydantic runs a later-declared model validator outside an earlier one, so
    declaring it last puts it outside the model's own ``after`` consistency
    checks, whose failures it must see.
    """

    return model_validator(mode="wrap")(classmethod(_drop_invalid_optional_evidence))


__all__ = [
    "HOST_PATH_MARKER",
    "EvidenceDrop",
    "EvidenceGroup",
    "OptionalEvidenceModel",
    "fail_open_on_optional_evidence",
    "redact_host_paths",
]
