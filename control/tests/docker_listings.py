"""A captured Docker listing must be read a line at a time.

``run_docker`` and ``run_with_timeout`` refuse any answer over
``MAX_COMMAND_OUTPUT_BYTES`` (4 KiB). A listing (``images``, ``image ls``,
``ps``, ``container ls``) grows with the very objects the helper has to
remove, so a captured listing ends every pass for good once it outgrows the
bound: both Sparks lost their image inventory this way and could never
collect again. ``run_docker_lines`` / ``run_lines`` bound each line and leave
the total to the command's deadline. A listing paged with ``--last`` is
bounded by its page and may stay captured.
"""

from __future__ import annotations

import re

KIND = "captured Docker listing; read it with run_docker_lines / run_lines"
SCOPE = "rust/crates/vonk-agent-helper/src/"
VERB = re.compile(r'"(?:images|ps)"|"(?:container|image)"\s*,\s*"ls"')
FUNCTION = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?fn\s", re.MULTILINE)
READER = re.compile(r"\brun(?:_docker)?_lines\s*\(")
CAPTURE = re.compile(
    r"\b(?:run_docker|run_docker_with_timeout|run|run_with_timeout|docker)\s*\("
)
PAGE = '"--last"'


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE) and path.endswith(".rs")


def scan_source(source: str, test_lines: set[int]) -> list[int]:
    starts = [match.start() for match in FUNCTION.finditer(source)] + [len(source)]
    hits = []
    for verb in VERB.finditer(source):
        line = source.count("\n", 0, verb.start()) + 1
        if line in test_lines:
            continue
        end = next(start for start in starts if start > verb.start())
        begin = max([start for start in starts if start <= verb.start()] or [0])
        body = source[begin:end]
        if CAPTURE.search(body) and not READER.search(body) and PAGE not in body:
            hits.append(line)
    return hits
