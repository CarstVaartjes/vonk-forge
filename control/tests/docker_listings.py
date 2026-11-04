"""A captured Docker answer that grows must be read a line at a time.

``run_docker``, ``run_docker_with_timeout`` and ``run_with_timeout`` refuse any
answer over ``MAX_COMMAND_OUTPUT_BYTES`` (4 KiB). Two kinds of answer grow past
it with the very objects the helper has to remove, and a captured one then ends
every pass for good:

* a listing (``images``, ``image ls``, ``ps``, ``container ls``);
* an inspection whose format has a ``{{range`` (one line per mount, and a
  model has one mount per file).

``run_docker_lines`` / ``run_lines`` bound each line and leave the total to the
command's deadline. Each call is judged on its own: a call that reads a single
exact target (``id=…``, ``name=^…$``) or one ``--last`` page is bounded by that
target or page and may stay captured. An argument vector built outside the call
is not seen; build it in the call.
"""

from __future__ import annotations

import re

KIND = "captured Docker listing or ranged inspection; use run_docker_lines / run_lines"
SCOPE = "rust/crates/vonk-agent-helper/src/"
CAPTURED = frozenset(
    {
        "run_docker",
        "run_docker_with_timeout",
        "run",
        "run_with_timeout",
        "docker",
        "container_fields",
    }
)
CALL = re.compile(r"\b(" + "|".join(sorted(CAPTURED)) + r")\s*\(")
LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
CONST_RANGE = re.compile(
    r'const\s+([A-Z][A-Z0-9_]*)\s*:\s*&str\s*=\s*"((?:[^"\\]|\\.)*)"\s*;'
)
LISTINGS = (("images",), ("ps",), ("image", "ls"), ("container", "ls"))


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE) and path.endswith(".rs")


def _arguments(source: str, start: int) -> str:
    """The text between the parenthesis at ``start - 1`` and its match."""
    depth, index, quoted = 1, start, False
    while index < len(source) and depth:
        char = source[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return source[start : index - 1]


def _verb(words: list[str]) -> bool:
    """Whether the leading words are a listing verb, or a ranged inspection."""
    return any(tuple(words[: len(verb)]) == verb for verb in LISTINGS)


def _bounded_target(words: list[str]) -> bool:
    return any(
        word == "--last"
        or word.startswith("id=")
        or (word.startswith("name=^") and word.endswith("$"))
        for word in words
    )


def _group(source: str, start: int) -> str:
    """The bracketed group opening at the first ``[`` or ``(`` from ``start``."""
    opener = re.compile(r"[\[(]").search(source, start)
    if opener is None:
        return ""
    close = {"[": "]", "(": ")"}[opener.group()]
    depth, index, quoted = 1, opener.end(), False
    while index < len(source) and depth:
        char = source[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == opener.group():
            depth += 1
        elif char == close:
            depth -= 1
        index += 1
    return source[opener.end() : index - 1]


def _words(source: str, call: re.Match[str], text: str) -> list[str]:
    """The call's string literals, or those of the vector it passes by name."""
    words = LITERAL.findall(text)
    if words:
        return words
    for name in re.findall(r"\b[a-z_][a-z0-9_]*\b", text):
        built = list(
            re.finditer(rf"\blet\s+(?:mut\s+)?{name}\b[^=;]*=", source[: call.start()])
        )
        if built:
            return LITERAL.findall(_group(source, built[-1].end()))
    return []


def scan_source(source: str, test_lines: set[int]) -> list[int]:
    ranged = {name for name, text in CONST_RANGE.findall(source) if "{{range" in text}
    hits = []
    for call in CALL.finditer(source):
        text = _arguments(source, call.end())
        words = _words(source, call, text)
        line = source.count("\n", 0, call.start()) + 1
        if line in test_lines or _bounded_target(words):
            continue
        inspects = (
            call.group(1) == "container_fields"
            or words[:2] == ["container", "inspect"]
            or words[:1] == ["inspect"]
        )
        grows = _verb(words) or (
            inspects
            and (
                any("{{range" in word for word in words)
                or any(re.search(rf"\b{name}\b", text) for name in ranged)
            )
        )
        if grows:
            hits.append(line)
    return hits
