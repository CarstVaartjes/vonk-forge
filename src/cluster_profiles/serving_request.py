"""The chat request a serving check is sent as, derived from the recipe's API facts.

A recipe declares a bounded chat check (``hello``, a few dozen tokens). Sent as
written to a route whose reasoning defaults on, the whole bounded reply is spent
thinking and no content arrives. The facts the recipe publishes beside it
(``recipe-api``: ``reasoning``, ``default_output_tokens``, ``max_output_tokens``)
say how to ask for a short answer. This is the one place that turns the declared
check into the request that fits those facts; it is the same rule the recipe
sweep applies (``spark_sweep/chat_probe.py::bounded_chat_body``):

* a ``switch`` route gets its declared OFF value under every declared key, at its
  declared placement (``chat_template_kwargs`` or top level), unless the request
  already names the key;
* an ``always-on`` route cannot turn thinking off, so it gets the route's own
  ``default_output_tokens``, capped by a numeric ``max_output_tokens``: a number
  is that number, an argument is the recipe's runtime argument of that name, and
  remaining context sends no ``max_tokens`` at all (the cap assertion goes with
  it: nothing was capped);
* a route without reasoning, or without facts, is sent as declared. A number
  the facts do not give is never invented.

The declared document is never edited: a copy is returned, so the recipe's
content identity does not change. The returned check is what is sent and what
the assertions are evaluated against.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vonk_forge_contracts.api import ChatApi

type Json = str | int | float | bool | None | list[Json] | dict[str, Json]

#: The value of the recipe's runtime argument of that name; None when it has no integer.
ArgumentValue = Callable[[str], int | None]


class ChatRequestKey(StrEnum):
    """The OpenAI chat request field this module reads and raises."""

    MAX_TOKENS = "max_tokens"


class ChatAssertion(StrEnum):
    OUTPUT_CAP = "chat.output-cap"


def _always_on_budget(
    api: ChatApi, argument_value: ArgumentValue | None
) -> tuple[bool, int | None]:
    """(is the budget known, the budget); remaining context is known and unbounded."""

    from vonk_forge_contracts.api import (
        ApiRemainingContext,
        ApiTokenArgument,
        ApiTokenValue,
    )

    default = api.default_output_tokens
    if isinstance(default, ApiRemainingContext):
        return True, None
    if isinstance(default, ApiTokenValue):
        budget: int | None = default.value
    elif isinstance(default, ApiTokenArgument) and argument_value is not None:
        budget = argument_value(default.argument)
    else:
        budget = None
    if budget is None:
        return False, None
    if isinstance(api.max_output_tokens, ApiTokenValue):
        budget = min(budget, api.max_output_tokens.value)
    return True, budget


def bounded_chat_check(
    check: Mapping[str, Json],
    api: ChatApi | None,
    argument_value: ArgumentValue | None = None,
) -> dict[str, Json]:
    """The check with its chat request fitted to the route's reasoning facts."""

    fitted = copy.deepcopy(dict(check))
    request = fitted.get("request")
    body = request.get("body") if isinstance(request, dict) else None
    if api is None or fitted.get("kind") != "openai.chat" or not isinstance(body, dict):
        return fitted
    from vonk_forge_contracts.api import ApiReasoningAlwaysOn, ApiReasoningSwitch

    reasoning = api.reasoning
    if isinstance(reasoning, ApiReasoningSwitch):
        target: Json = body
        if reasoning.via == "chat_template_kwargs":
            target = body.setdefault("chat_template_kwargs", {})
        if isinstance(target, dict):
            for key in reasoning.keys:
                target.setdefault(key, reasoning.off)
    elif isinstance(reasoning, ApiReasoningAlwaysOn):
        known, budget = _always_on_budget(api, argument_value)
        if not known:
            return fitted
        if budget is None:
            body.pop(ChatRequestKey.MAX_TOKENS, None)
            assertions = fitted.get("assertions")
            if isinstance(assertions, list):
                fitted["assertions"] = [
                    item for item in assertions if item != ChatAssertion.OUTPUT_CAP
                ]
        else:
            declared = body.get(ChatRequestKey.MAX_TOKENS)
            body[ChatRequestKey.MAX_TOKENS] = max(
                declared if type(declared) is int else 0, budget
            )
    return fitted
