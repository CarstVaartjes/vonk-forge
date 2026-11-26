"""Which closed word sets in a Controller response vonkctl decides by.

vonkctl reads every Controller response against the OpenAPI it ships
(``schemas/control-openapi.json``) and the Python client generated from that
copy. A closed word set it only shows is open there: a newer Controller that
adds a word (a reason, attention or wait-owner code) is read, and the word is
shown as sent. The sets it decides by stay closed, so a word it does not know
there is a contract mismatch, answered by ``vonkctl update --apply``.

This is forward tolerance in the reader, not a shim for an older Controller:
the CLI keeps one contract and one reading path.

``scripts/generate-control-clients`` opens the shipped copy and the client's
input with :func:`open_shown_words`; the Controller's own document stays exact.
``tests/test_generated_contracts.py`` keeps :data:`DECIDED_BY` equal to the
sets vonkctl compares outside its renderers.

Stdlib only: the generator imports this before the generated client exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

type _Json = str | int | float | bool | list[_Json] | dict[str, _Json] | None

#: Response word sets vonkctl decides by: a word chooses what it does (when a
#: follow ends, how a refusal is disposed of, whether an enrollment, a run or a
#: profile load still has work). Only a set that a response field holds on its
#: own is named; one held only inside a union of reason sets reads with it.
DECIDED_BY: frozenset[str] = frozenset(
    {
        "Disposition",
        "EndpointState",
        "EnrollmentGrantState",
        "JobContractState",
        "LifecycleState",
        "ProfileLoadAction",
        "RunState",
    }
)

#: The schema of an opened word set: any non-empty word of a reason-code length.
_SHOWN_WORD: dict[str, str | int] = {"type": "string", "minLength": 1, "maxLength": 128}

_REFERENCE = "#/components/schemas/"


def _references(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith(_REFERENCE):
            yield reference.removeprefix(_REFERENCE)
        for value in node.values():
            yield from _references(value)
    elif isinstance(node, list):
        for value in node:
            yield from _references(value)


def _schemas(document: object) -> dict[str, _Json]:
    components = document.get("components") if isinstance(document, dict) else None
    schemas = components.get("schemas") if isinstance(components, dict) else None
    return schemas if isinstance(schemas, dict) else {}


def _reachable(schemas: dict[str, _Json], roots: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(_references(schemas.get(name)))
    return seen


def received_schemas(document: object) -> frozenset[str]:
    """Components a response can carry and no request or parameter can.

    A component a request also carries is authored by the CLI and stays closed.
    """

    sent: list[str] = []
    received: list[str] = []
    paths = document.get("paths") if isinstance(document, dict) else None
    for item in paths.values() if isinstance(paths, dict) else ():
        for operation in item.values() if isinstance(item, dict) else ():
            if isinstance(operation, dict):
                received.extend(_references(operation.get("responses")))
                received.extend(
                    _references(operation.get("x-vonk-observation-payload"))
                )
                sent.extend(_references(operation.get("requestBody")))
                sent.extend(_references(operation.get("parameters")))
    schemas = _schemas(document)
    return frozenset(_reachable(schemas, received) - _reachable(schemas, sent))


def response_word_sets(document: object) -> frozenset[str]:
    """The closed word sets among :func:`received_schemas`."""

    schemas = _schemas(document)
    return frozenset(
        name
        for name in received_schemas(document)
        if isinstance(shown := schemas.get(name), dict) and "enum" in shown
    )


def open_shown_words(document: object) -> None:
    """Open, in place, every response word set of ``document`` vonkctl only shows."""

    schemas = _schemas(document)
    for name in response_word_sets(document) - DECIDED_BY:
        shown = schemas[name]
        kept = (
            {key: shown[key] for key in ("title", "description") if key in shown}
            if isinstance(shown, dict)
            else {}
        )
        schemas[name] = {**kept, **_SHOWN_WORD}
