"""Resolve the pure-Python protocol wheel from its package metadata.

The dependency, uv lock and Docker context filter are deterministic projections.
Run with --write-projections after changing the owning project metadata, then
build the wheel and refresh its content pin with refresh-protocol-wheel-lock.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

PROJECTIONS = (
    "control/pyproject.toml",
    "control/uv.lock",
    "agent_protocol/uv.lock",
    ".dockerignore",
)


def package_name(root: Path) -> str:
    return tomllib.loads((root / "agent_protocol/pyproject.toml").read_text())[
        "project"
    ]["name"]


def package_version(root: Path) -> str:
    return tomllib.loads((root / "agent_protocol/pyproject.toml").read_text())[
        "project"
    ]["version"]


def wheel_filename(root: Path) -> str:
    name = re.sub(r"[-_.]+", "_", package_name(root))
    version = package_version(root)
    if re.fullmatch(r"[0-9][0-9A-Za-z.!+]*", version) is None:
        raise ValueError("invalid protocol package version")
    return f"{name}-{version}-py3-none-any.whl"


def wheel_relative(root: Path) -> str:
    return f"inventory/wheels/{wheel_filename(root)}"


def _projection_patterns(root: Path, file: str) -> list[tuple[str, str]]:
    name = package_name(root)
    version = package_version(root)
    path = "../" + wheel_relative(root)
    escaped = re.escape(name)
    if file == "agent_protocol/uv.lock":
        return []
    if file == "control/pyproject.toml":
        return [
            (rf'  "{escaped}==[^"\n]+",', f'  "{name}=={version}",'),
            (rf"{escaped} = .*", f'{name} = {{ path = "{path}" }}'),
        ]
    if file == "control/uv.lock":
        return [
            (
                r'source = \{ path = "../inventory/wheels/[^"\n]+" \}',
                f'source = {{ path = "{path}" }}',
            ),
            (
                r'    \{ filename = "[^"\n]+", hash = "sha256:([0-9a-f]+)" \},',
                f'    {{ filename = "{wheel_filename(root)}", hash = "sha256:HASH" }},',
            ),
            (
                rf'    \{{ name = "{escaped}", path = "[^"\n]+" \}},',
                f'    {{ name = "{name}", path = "{path}" }},',
            ),
        ]
    if file == ".dockerignore":
        return [
            (
                r"!inventory/wheels/"
                + re.escape(re.sub(r"[-_.]+", "_", name))
                + r"-[^\n]+",
                "!" + wheel_relative(root),
            )
        ]
    raise ValueError(f"unregistered protocol projection: {file}")


def project(root: Path, file: str, text: str) -> str:
    """Reproduce only registered projections, preserving unrelated dependency selections."""
    for pattern, replacement in _projection_patterns(root, file):
        matches = list(re.finditer("(?m)^" + pattern + "$", text))
        if len(matches) != 1:
            raise ValueError(
                f"{file}: missing or duplicate protocol projection: {pattern}"
            )
        match = matches[0]
        if "HASH" in replacement:
            replacement = replacement.replace("HASH", match[1])
        text = text[: match.start()] + replacement + text[match.end() :]
    if file in ("control/uv.lock", "agent_protocol/uv.lock"):
        text, count = re.subn(
            rf'(\[\[package\]\]\nname = "{re.escape(package_name(root))}"\nversion = ")[^"]+(")',
            lambda m: m[1] + package_version(root) + m[2],
            text,
        )
        if count != 1:
            raise ValueError("missing or duplicate protocol package version")
    return text


def projection_lines(root: Path, file: str, text: str) -> set[int]:
    generated = project(root, file, text)
    return {
        text.count("\n", 0, match.start()) + 1
        for pattern, _ in _projection_patterns(root, file)
        for match in re.finditer("(?m)^" + pattern + "$", generated)
        if text.splitlines()[text.count("\n", 0, match.start())] == match.group()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--filename", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--write-projections", action="store_true")
    args = parser.parse_args()
    if args.write_projections:
        for file in PROJECTIONS:
            path = args.root / file
            path.write_text(project(args.root, file, path.read_text()))
    elif args.filename:
        print(wheel_filename(args.root))
    elif args.directory:
        print(args.directory / wheel_filename(args.root))
    else:
        print(wheel_relative(args.root))


if __name__ == "__main__":
    main()
