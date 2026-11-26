"""Which closed word sets in a Controller response vonkctl decides by.

vonkctl reads every Controller response against the OpenAPI it ships
(``schemas/control-openapi.json``) and the Python client generated from that
copy. A closed word set it only shows is open there: a newer Controller that
adds a word (a reason, attention or wait-owner code) is read, and the word is
shown as sent. The sets it decides by stay closed, so a word it does not know
there is a contract mismatch, answered by ``vonkctl update --apply``.

Likewise a response object is open there: the Controller's strict response
models carry ``additionalProperties: false``, which would refuse every optional
field a newer Controller adds, so the shipped copy drops it on every component
a response carries, including one a request also carries (as
``tools/vonkctl_json_contract.py`` does for the ``--json`` contract). The
Controller still validates requests strictly, and the CLI builds them from its
attrs models. A word set a request also carries stays closed. This keeps the
stable-channel compatibility gate (``tools/client_compatibility_report.py``)
firing only on real breaks.

This is forward tolerance in the reader, not a shim for an older Controller:
the CLI keeps one contract and one reading path.

``scripts/dev/generate-control-clients`` opens the shipped copy and the client's
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

#: The schema of an opened word set: any non-empty word. The response document
#: bound (``MAX_CONTROL_DOCUMENT_BYTES``) already bounds its length.
_SHOWN_WORD: dict[str, str | int] = {"type": "string", "minLength": 1}

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


def _roots(document: object) -> tuple[list[str], list[str]]:
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
    return received, sent


def response_schemas(document: object) -> frozenset[str]:
    """Every component a response can carry, including one a request also carries."""

    return frozenset(_reachable(_schemas(document), _roots(document)[0]))


def received_schemas(document: object) -> frozenset[str]:
    """Components a response can carry and no request or parameter can.

    A word set a request also carries is authored by the CLI and stays closed.
    """

    received, sent = _roots(document)
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


def _drop_closed_objects(node: _Json) -> None:
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            del node["additionalProperties"]
        for value in node.values():
            _drop_closed_objects(value)
    elif isinstance(node, list):
        for value in node:
            _drop_closed_objects(value)


def open_shown_words(document: object) -> None:
    """Open, in place, what vonkctl only reads of a response in ``document``.

    That is every response word set it only shows, and the closing of every
    response object.
    """

    schemas = _schemas(document)
    for name in response_schemas(document):
        _drop_closed_objects(schemas.get(name))
    for name in response_word_sets(document) - DECIDED_BY:
        shown = schemas[name]
        kept = (
            {key: shown[key] for key in ("title", "description") if key in shown}
            if isinstance(shown, dict)
            else {}
        )
        schemas[name] = {**kept, **_SHOWN_WORD}
