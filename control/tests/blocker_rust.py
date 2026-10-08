"""Token-based inventory of explicit Rust operation endings.

This is a conservative inventory, not a Rust type checker. Every Err constructor
is counted (including tail expressions and closures), as are bail!/ensure! and
process exit/abort calls and failure ExitCode values. Propagation with ? is not a new refusal site. Comments,
normal/byte/raw strings, character literals and test-only items are skipped;
lifetimes remain tokens. No error name or message establishes a security edge
or proves recovery: unreviewed sites are bookkeeping debt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .blocker_boundaries import REPO_ROOT, RaiseSite

RUST_CRATES = (
    "vonk-agent",
    "vonk-agent-helper",
    "vonk-build-egress",
    "vonk-spark-setup",
    "vonk-nas-setup",
    "vonk-monitor",
)
RUST_ROOTS = tuple(REPO_ROOT / "rust" / "crates" / name / "src" for name in RUST_CRATES)


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    literal: bool = False


_RAW = re.compile(r'(?:br|cr|r)(#+)?"')
_CHAR = re.compile(r"(?:b)?'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'")
_WORD = re.compile(r"(?:r#)?[A-Za-z_][A-Za-z_0-9]*|[0-9][A-Za-z_0-9]*|::|->|=>")


def tokenize(source: str) -> list[Token]:
    """Lex Rust token trees; malformed supplied source is rejected, never omitted."""
    tokens: list[Token] = []
    i, line = 0, 1
    while i < len(source):
        start, at = i, line
        if source[i].isspace():
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            i += 2
            depth = 1
            while i < len(source) and depth:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError("unterminated Rust comment")
        elif raw := _RAW.match(source, i):
            delimiter = '"' + (raw[1] or "")
            body = i + len(raw[0])
            end = source.find(delimiter, body)
            if end < 0:
                raise ValueError("unterminated Rust raw literal")
            i = end + len(delimiter)
            tokens.append(Token(source[body:end], at, True))
        elif source[i] == '"' or source.startswith(('b"', 'c"'), i):
            if source[i] != '"':
                i += 1
            body = i + 1
            i = body
            while i < len(source) and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
            if i >= len(source):
                raise ValueError("unterminated Rust string")
            tokens.append(Token(source[body:i], at, True))
            i += 1
        elif char := _CHAR.match(source, i):
            i += len(char[0])
            tokens.append(Token(char[0], at, True))
        elif word := _WORD.match(source, i):
            i += len(word[0])
            tokens.append(Token(word[0], at))
        else:
            tokens.append(Token(source[i], at))
            i += 1
        line += source[start:i].count("\n")
    return tokens


def _pairs(tokens: list[Token]) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    closing = {"}": "{", ")": "(", "]": "["}
    for i, token in enumerate(tokens):
        if token.literal:
            continue
        if token.text in closing:
            if not stack or tokens[stack[-1]].text != closing[token.text]:
                raise ValueError("unbalanced Rust token tree")
            start = stack.pop()
            pairs[start] = i
        elif token.text in closing.values():
            stack.append(i)
    if stack:
        raise ValueError("unclosed Rust token tree")
    return pairs


def scan_rust_source(source: str, *, path: str) -> list[RaiseSite]:
    tokens = tokenize(source)
    pairs = _pairs(tokens)
    texts = [token.text if not token.literal else "<literal>" for token in tokens]
    functions: list[tuple[int, int, str]] = []
    excluded: set[int] = set()
    for i, text in enumerate(texts):
        if text == "#" and texts[i + 1 : i + 2] == ["["]:
            end = pairs[i + 1]
            attribute = texts[i + 2 : end]
            if attribute == ["cfg", "(", "test", ")"] or attribute in (
                ["test"],
                ["tokio", "::", "test"],
            ):
                j = end + 1
                while j < len(tokens) and texts[j] not in {"{", ";"}:
                    if j in pairs:
                        j = pairs[j]
                    j += 1
                if j in pairs:
                    excluded.update(range(i, pairs[j] + 1))
        if text == "fn" and i + 1 < len(tokens):
            j = i + 2
            while j < len(tokens) and texts[j] not in {"{", ";"}:
                if j in pairs:
                    j = pairs[j]
                j += 1
            if j in pairs:
                functions.append((j, pairs[j], texts[i + 1]))
    aliases = {
        "Err": "Err",
        "bail": "bail",
        "ensure": "ensure",
        "exit": "exit",
        "abort": "abort",
    }
    for i, text in enumerate(texts):
        if text == "as" and i > 0 and i + 1 < len(texts) and texts[i - 1] in aliases:
            aliases[texts[i + 1]] = aliases[texts[i - 1]]
    sites: list[RaiseSite] = []
    for i, text in enumerate(texts):
        if i in excluded:
            continue
        text = aliases.get(text, text)
        kind = ""
        opening = i + 1
        if text == "Err" and texts[opening : opening + 2] == ["::", "<"]:
            opening += 2
            depth = 1
            while opening < len(tokens) and depth:
                depth += int(texts[opening] == "<") - int(texts[opening] == ">")
                opening += 1
        if text in {"bail", "ensure"} and texts[i + 1 : i + 2] == ["!"]:
            kind, opening = text + "!", i + 2
        elif text == "Err" and texts[opening : opening + 1] == ["("]:
            kind = "Err"
        elif text == "ExitCode" and texts[i + 1 : i + 4] == ["::", "from", "("]:
            kind, opening = "ExitCode::from", i + 3
        elif (
            text in {"exit", "abort"}
            and texts[i + 1 : i + 2] == ["("]
            and (i == 0 or texts[i - 1] != ".")
        ):
            # Qualified process calls, or directly imported exit/abort. Counting
            # an identically named local function is conservative, not omission.
            kind = "process::" + text
        if text == "ExitCode" and texts[i + 1 : i + 3] == ["::", "FAILURE"]:
            function = ".".join(
                name for start, stop, name in functions if start < i < stop
            )
            sites.append(
                RaiseSite(
                    path,
                    "ExitCode::FAILURE",
                    function or "<module>",
                    "FAILURE",
                    tokens[i].line,
                )
            )
        if not kind or opening not in pairs:
            continue
        end = pairs[opening]
        if kind in {"process::exit", "ExitCode::from"} and texts[opening + 1 : end] == [
            "0"
        ]:
            continue
        if kind == "Err":
            # Match arms, let/if-let/while-let patterns and alternatives construct
            # nothing. Guarded match arms also end at =>, not a statement.
            if texts[end + 1 : end + 2] in (["=>"], ["="], ["|"], ["if"]):
                continue
            if i > 0 and texts[i - 1] == "|":
                continue
        argument = tokens[opening + 1 : end]
        # Stable token identity rather than line numbers: moves do not erase debt.
        # Literal text participates so distinct refusals cannot share a count.
        code = " ".join(token.text for token in argument)
        message = " ".join(token.text for token in argument if token.literal)
        function = ".".join(name for start, stop, name in functions if start < i < stop)
        sites.append(
            RaiseSite(path, kind, function or "<module>", code, tokens[i].line, message)
        )
    return sites


@lru_cache(maxsize=512)
def _scan_file(module: Path, stamp: tuple[int, int]) -> tuple[RaiseSite, ...]:
    return tuple(
        scan_rust_source(
            module.read_text(encoding="utf-8"),
            path=module.relative_to(REPO_ROOT).as_posix(),
        )
    )


@lru_cache(maxsize=512)
def _test_module_roots(module: Path, stamp: tuple[int, int]) -> tuple[Path, ...]:
    """Resolve external cfg(test) modules using Rust's module-file layout."""
    tokens = tokenize(module.read_text(encoding="utf-8"))
    texts = [token.text if not token.literal else "<literal>" for token in tokens]
    base = (
        module.parent
        if module.name in {"lib.rs", "main.rs", "mod.rs"}
        else module.with_suffix("")
    )
    roots = []
    for i in range(len(texts) - 9):
        if (
            texts[i : i + 8] == ["#", "[", "cfg", "(", "test", ")", "]", "mod"]
            and texts[i + 9] == ";"
        ):
            roots.extend((base / (texts[i + 8] + ".rs"), base / texts[i + 8]))
    return tuple(roots)


def scan_rust_raises(roots: tuple[Path, ...] = RUST_ROOTS) -> list[RaiseSite]:
    sites: list[RaiseSite] = []
    for root in roots:
        modules = sorted(root.rglob("*.rs"))
        stamps = {
            module: (module.stat().st_size, module.stat().st_mtime_ns)
            for module in modules
        }
        excluded = tuple(
            path
            for module in modules
            for path in _test_module_roots(module, stamps[module])
        )
        for module in modules:
            if any(module == path or module.is_relative_to(path) for path in excluded):
                continue
            if module.name in {"tests.rs", "test_support.rs"} or {
                "tests",
                "test_support",
            }.intersection(module.relative_to(root).parts):
                continue
            sites.extend(_scan_file(module, stamps[module]))
    return sites
