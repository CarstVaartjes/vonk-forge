"""Optional agent evidence never fails the mandatory part of a report.

An agent report has a mandatory core (identity, lease, capacity, the outcome of
an operation) and optional evidence (NICs, the NAS route, fabric details,
sensor readings, progress, failure diagnostics). Refusing the whole report for
a damaged optional part loses the core: an inventory stops updating, a result
is lost with its lease. A model that mixes in :class:`OptionalEvidenceModel`
names its optional evidence as groups of field paths; when validation fails and
dropping some groups makes it pass, the groups are dropped and the model carries
one typed :class:`AgentEvidenceCode` per dropped group. When the core itself is
invalid the original error is raised unchanged.

This is confined to evidence the Controller only displays or ignores. Identity,
time, fences, fabric *policy* decisions, versions and every other mandatory
field stay strict, and nothing is repaired into a different valid value: a
damaged optional part is removed, never rewritten.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Mapping
from typing import Any, ClassVar, NamedTuple

from pydantic import BaseModel, PrivateAttr, ValidationError, model_validator
from pydantic_core.core_schema import ValidatorFunctionWrapHandler

from .reason_codes import AgentEvidenceCode


class EvidenceGroup(NamedTuple):
    """Optional fields that are valid together or dropped together."""

    code: AgentEvidenceCode
    #: Each path is the keys from the model's input down to one optional field.
    paths: tuple[tuple[str, ...], ...]


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


class OptionalEvidenceModel(BaseModel):
    """Mixin: drop invalid optional evidence instead of refusing the report."""

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = ()
    _evidence_warnings: tuple[AgentEvidenceCode, ...] = PrivateAttr(default=())

    @property
    def evidence_warnings(self) -> tuple[AgentEvidenceCode, ...]:
        """The optional evidence this report lost, empty when it arrived whole."""

        return self._evidence_warnings

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
    except ValidationError:
        if not isinstance(data, Mapping):
            raise
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
                model._evidence_warnings = tuple(group.code for group in dropped)
                return model
        raise


def fail_open_on_optional_evidence() -> Any:
    """The wrap validator to assign, last, in the body of a model with groups.

    Pydantic runs a later-declared model validator outside an earlier one, so
    declaring it last puts it outside the model's own ``after`` consistency
    checks, whose failures it must see.
    """

    return model_validator(mode="wrap")(classmethod(_drop_invalid_optional_evidence))


__all__ = ["EvidenceGroup", "OptionalEvidenceModel", "fail_open_on_optional_evidence"]
