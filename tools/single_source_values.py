"""Owner-adjacent registrations for handwritten literal families, without debt allowances."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from control.tests.parsed_sources import parse_file
from scripts.protocol_wheel import project, projection_lines


class Family(StrEnum):
    FLOATING_TAGS = "floating_tags"
    PROTOCOL_WHEEL = "protocol_wheel"
    MIRROR_INPUT = "mirror_input"


class SingleSourceFixtureException(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file: str
    function: str
    reason: str


class SingleSourceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    semantic_id: str
    owner: str
    selector: str
    family: Family
    projections: tuple[str, ...] = ()
    exceptions: tuple[SingleSourceFixtureException, ...] = ()


class SingleSourceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[SingleSourceRegistration]


def tracked_files(root: Path) -> list[str]:
    return (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=root, timeout=30)
        .decode()
        .split("\0")[:-1]
    )


def registrations(root: Path, files: list[str]) -> list[SingleSourceRegistration]:
    values = [
        value
        for file in files
        if file.endswith("/single-source-values.toml")
        for value in SingleSourceRegistry.model_validate(
            tomllib.loads((root / file).read_text())
        ).values
    ]
    identities = [(value.owner, value.selector) for value in values]
    if len({value.semantic_id for value in values}) != len(values):
        raise ValueError("duplicate semantic ID")
    if len(set(identities)) != len(values) or len({v.family for v in values}) != len(
        values
    ):
        raise ValueError("overlapping registrations or duplicate owners")
    return values


def _owner_assignment(root: Path, value: SingleSourceRegistration) -> ast.Assign:
    definitions = [
        node
        for node in parse_file(root / value.owner).tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == value.selector for t in node.targets
        )
    ]
    if len(definitions) != 1:
        raise ValueError(f"missing or ambiguous owner selector: {value.selector}")
    return definitions[0]


def owner_value(root: Path, value: SingleSourceRegistration) -> object:
    path = root / value.owner
    if value.family == Family.FLOATING_TAGS:
        definition = _owner_assignment(root, value).value
        if isinstance(definition, ast.Call):
            definition = definition.args[0]
        return ast.literal_eval(definition)
    text = path.read_text()
    document = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
    for key in value.selector.split("."):
        document = document[key]
    return document


def _executable_text(file: str, text: str) -> str:
    if not file.endswith(".md"):
        return text
    # Preserve line numbers while scanning executable documentation fences.
    inside = False
    lines = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            inside = not inside
            lines.append("\n")
        else:
            lines.append(line if inside else "\n")
    return "".join(lines)


def _fixture_lines(root: Path, exception: SingleSourceFixtureException) -> range:
    if (
        not exception.file.startswith(("tests/", "control/tests/"))
        or not exception.function.startswith("test_")
        or not exception.reason.strip()
        or any(c in exception.file + exception.function for c in "*?[")
    ):
        raise ValueError(
            "fixture exceptions require an exact adversarial test function and reason"
        )
    functions = [
        node
        for node in ast.walk(parse_file(root / exception.file).tree)
        if isinstance(node, ast.FunctionDef) and node.name == exception.function
    ]
    if len(functions) != 1:
        raise ValueError(f"missing or ambiguous fixture: {exception.function}")
    function = functions[0]
    return range(
        min([function.lineno, *[d.lineno for d in function.decorator_list]]),
        (function.end_lineno or function.lineno) + 1,
    )


def violations(root: Path, files: list[str] | None = None) -> list[str]:
    files = tracked_files(root) if files is None else files
    errors: list[str] = []
    for value in registrations(root, files):
        owned = owner_value(
            root, value
        )  # Missing selectors must fail, never skip a class.
        exceptions = [(e.file, _fixture_lines(root, e)) for e in value.exceptions]
        if value.projections:
            if value.family != Family.PROTOCOL_WHEEL:
                raise ValueError(
                    "only reproducible protocol projections are registered"
                )
            for file in value.projections:
                text = (root / file).read_text()
                if project(root, file, text) != text:
                    errors.append(
                        f"{value.semantic_id}: {file}:1: generated bytes differ; run python3 scripts/protocol_wheel.py --write-projections"
                    )
        owner_lines: range = range(0)
        if value.family == Family.FLOATING_TAGS:
            assignment = _owner_assignment(root, value)
            owner_lines = range(
                assignment.lineno, (assignment.end_lineno or assignment.lineno) + 1
            )
        for file in files:
            try:
                text = _executable_text(file, (root / file).read_text())
            except (UnicodeError, IsADirectoryError):
                continue
            generated_lines = (
                projection_lines(root, file, text)
                if file in value.projections
                else set()
            )
            matches: list[tuple[int, str]] = []
            if value.family == Family.PROTOCOL_WHEEL:
                # The whole filename family, including old and future versions.
                matches = [
                    (m.start(), m.group())
                    for m in re.finditer(r"vonk_agent_protocol-[\w.+!-]+\.whl", text)
                ]
            elif value.family == Family.FLOATING_TAGS:
                assert isinstance(owned, (set, frozenset))
                for match in re.finditer(r"[\[{(][^\[\]{}()]*[\]})]", text):
                    try:
                        collection = ast.literal_eval(match.group())
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(collection, (set, tuple, list, dict)) and (
                        set(collection) == owned
                        or bool(set(collection) & owned)
                        and re.search(
                            r"(?i)\b\w*float\w*\s*=",
                            text[
                                text.rfind("\n", 0, match.start()) + 1 : match.start()
                            ],
                        )
                        is not None
                    ):
                        matches.append((match.start(), match.group()))
            elif value.family == Family.MIRROR_INPUT and (
                file == ".github/workflows/ci-image-mirror.yml"
                or "mirror-inputs" in file
            ):
                matches = [
                    (m.start(), m.group())
                    for m in re.finditer(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", text)
                ]
            for offset, observed in matches:
                line = text.count("\n", 0, offset) + 1
                if (
                    (file == value.owner and line in owner_lines)
                    or line in generated_lines
                    or any(file == path and line in lines for path, lines in exceptions)
                ):
                    continue
                errors.append(
                    f"{value.semantic_id}: {file}:{line}: {observed!r}; owner {value.owner}:{value.selector}; derive from owner"
                )
    return errors
