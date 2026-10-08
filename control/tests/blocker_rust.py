"""Token-based inventory of explicit Rust operation endings.

This is a conservative inventory, not a Rust type checker. Every Err constructor
is counted (including tail expressions and closures), as are bail!/ensure! and
process exit/abort calls and failure ExitCode values. Propagation with ? is not a new refusal site. Comments,
normal/byte/raw strings, character literals and test-only items are skipped;
lifetimes remain tokens. No error name or message establishes a security edge
or proves recovery: unreviewed sites are bookkeeping debt.

Coverage is the union of Cargo targets and declared source modules, including
literal includes and literal cfg_attr path variants. It is not rustc expansion:
computed includes and unavailable build outputs end unknown without publishing
a partial inventory. A six-crate root list is not proof of compiler coverage.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .blocker_boundaries import REPO_ROOT, RaiseSite
from .source_observation import observe

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


def rust_identities(source: str) -> dict[str, str]:
    """Bind findings to complete callable content, independent of file location."""
    tokens = tokenize(source)
    pairs = _pairs(tokens)
    result: dict[str, str] = {}
    for i, token in enumerate(tokens):
        if token.literal or token.text != "fn" or i + 1 >= len(tokens):
            continue
        j = i + 2
        while j < len(tokens) and tokens[j].text not in {"{", ";"}:
            j = pairs[j] + 1 if j in pairs else j + 1
        if j in pairs:
            body = [(t.text, t.literal) for t in tokens[i : pairs[j] + 1]]
            # Repeated method names have no unique proof identity.
            name = tokens[i + 1].text
            digest = hashlib.sha256(repr(body).encode()).hexdigest()
            result[name] = digest if name not in result else ""
    return result


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
        # Match/let patterns construct nothing. A preceding | can also be
        # a closure delimiter; only the following pattern syntax excludes Err.
        if kind == "Err" and texts[end + 1 : end + 2] in (["=>"], ["="], ["|"], ["if"]):
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


def _module_files(
    module: Path, source: str, *, crate_root: bool = False
) -> tuple[Path, ...]:
    """Follow production module declarations, including visibility and path attrs.

    cfg(test) membership belongs to the declaration, never its filename. Other
    cfg predicates are included conservatively across supported platform builds.
    A missing declared module is an incomplete observation, not an empty module.
    """
    tokens = tokenize(source)
    pairs = _pairs(tokens)
    texts = [token.text if not token.literal else "<literal>" for token in tokens]
    base = (
        module.parent
        if crate_root or module.name in {"lib.rs", "main.rs", "mod.rs"}
        else module.with_suffix("")
    )
    files: list[Path] = []

    def visit(start: int, stop: int, directory: Path) -> None:
        i = start
        attributes: list[list[Token]] = []
        while i < stop:
            if texts[i : i + 2] == ["#", "["]:
                end = pairs[i + 1]
                attribute = tokens[i + 2 : end]
                attributes.append(attribute)
                i = end + 1
                continue
            if texts[i] == "pub":
                i += 1
                if texts[i : i + 1] == ["("]:
                    i = pairs[i] + 1
                continue
            if texts[i] == "mod" and i + 2 < stop:
                name, delimiter = texts[i + 1 : i + 3]
                test_only = any(
                    [token.text for token in attribute] == ["cfg", "(", "test", ")"]
                    for attribute in attributes
                )
                overrides = [
                    attribute[2].text
                    for attribute in attributes
                    if len(attribute) == 3
                    and [token.text for token in attribute[:2]] == ["path", "="]
                    and attribute[2].literal
                ]
                conditional_paths = [
                    attribute[j + 2].text
                    for attribute in attributes
                    if attribute and attribute[0].text == "cfg_attr"
                    for j in range(len(attribute) - 2)
                    if attribute[j].text == "path"
                    and attribute[j + 1].text == "="
                    and attribute[j + 2].literal
                ]
                if delimiter == ";" and not test_only:
                    files.extend(directory / path for path in conditional_paths)
                    if overrides:
                        files.append(directory / overrides[-1])
                    else:
                        direct = directory / (name + ".rs")
                        nested = directory / name / "mod.rs"
                        # Choosing the declared path retains disappearance as a
                        # read failure; it cannot silently erase its endings.
                        files.append(direct if direct.exists() else nested)
                elif delimiter == "{" and not test_only:
                    visit(i + 3, pairs[i + 2], directory / name)
                if delimiter == "{":
                    i = pairs[i + 2] + 1
                else:
                    i += 3
                attributes = []
                continue
            if texts[i : i + 2] == ["include", "!"]:
                opening = i + 2
                if opening not in pairs:
                    raise ValueError("incomplete included Rust source")
                argument = tokens[opening + 1 : pairs[opening]]
                if len(argument) != 1 or not argument[0].literal:
                    raise ValueError("computed include requires build output evidence")
                files.append(module.parent / argument[0].text)
                i = pairs[opening] + 1
                attributes = []
                continue
            if i in pairs and texts[i] == "{":
                test_only = any(
                    [t.text for t in attribute]
                    in (["test"], ["cfg", "(", "test", ")"], ["tokio", "::", "test"])
                    for attribute in attributes
                )
                if not test_only:
                    visit(i + 1, pairs[i], directory)
                i = pairs[i] + 1
                attributes = []
                continue
            # Retain attributes through function signatures until their body.
            if texts[i] == ";":
                attributes = []
            i = pairs[i] + 1 if i in pairs else i + 1

    visit(0, len(tokens), base)
    return tuple(path.resolve() for path in files)


def _crate_entries(root: Path) -> tuple[Path, ...]:
    entries = {root / name for name in ("lib.rs", "main.rs") if (root / name).is_file()}
    manifest = root.parent / "Cargo.toml"
    if not manifest.is_file():
        raise FileNotFoundError(manifest)
    document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    for target in [document.get("lib", {}), *document.get("bin", [])]:
        if "path" in target:
            entries.add(root.parent / target["path"])
    if document.get("package", {}).get("autobins", True):
        entries.update((root / "bin").glob("*.rs"))
        entries.update((root / "bin").glob("*/main.rs"))
    if not entries:
        raise FileNotFoundError(root)
    return tuple(sorted(entry.resolve() for entry in entries))


def _scan_rust_raises_once(roots: tuple[Path, ...]) -> list[RaiseSite]:
    sites: list[RaiseSite] = []
    visited: set[Path] = set()
    entries = {entry for root in roots for entry in _crate_entries(root)}
    pending = sorted(entries)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        source = module.read_text(encoding="utf-8")
        # One read supplies membership and endings, so an intervening file
        # replacement cannot mix two observations of the same module.
        sites.extend(
            scan_rust_source(source, path=module.relative_to(REPO_ROOT).as_posix())
        )
        pending.extend(_module_files(module, source, crate_root=module in entries))
    return sorted(sites, key=lambda site: (site.path, site.line))


def scan_rust_raises(roots: tuple[Path, ...] = RUST_ROOTS) -> list[RaiseSite]:
    """Reread incomplete observations; never publish a partial/zero inventory.

    This read-only owner allows three complete observation attempts. Exhaustion
    carries the shared unknown outcome, with no persisted state or busy marker;
    a fresh invocation starts its own budget against the current source bytes.
    """
    return observe(lambda: _scan_rust_raises_once(roots))
