"""The shipped CLI runbook stays parseable by the installed wheel."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_REDIRECTION = re.compile(r"^(?:\d+)?(?:>>?|<<?|&>|>&|<&)")
_SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PLACEHOLDER_UUID = "11111111-1111-4111-8111-111111111111"
_PLACEHOLDER_DIGEST = "a" * 64


def _runbook_examples(path: Path) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    in_code_block = False
    in_shell_block = False
    pending = ""
    pending_line = 0

    def add_command(line_number: int, source: str) -> None:
        try:
            tokens = shlex.split(source, comments=True, posix=True)
        except ValueError as error:
            raise AssertionError(
                f"{path}:{line_number}: invalid shell example: {error}"
            ) from error
        try:
            command_index = tokens.index("vonkctl")
        except ValueError:
            return
        prefix = tokens[:command_index]
        if prefix and not (
            (
                prefix[0] == "env"
                and all(_SHELL_ASSIGNMENT.match(item) for item in prefix[1:])
            )
            or all(_SHELL_ASSIGNMENT.match(item) for item in prefix)
        ):
            return

        arguments: list[str] = []
        for token in tokens[command_index + 1 :]:
            if _REDIRECTION.match(token):
                break
            if token in {"&&", "||", ";", "|", "&"}:
                break
            if token in {"REVIEWED_DIGEST", "$REVIEWED_DIGEST"}:
                token = _PLACEHOLDER_DIGEST
            elif re.fullmatch(r"[A-Z][A-Z0-9_]*(?:_UUID|_ID)", token):
                token = _PLACEHOLDER_UUID
            arguments.append(token)
        examples.append(
            {
                "line": line_number,
                "source": source,
                "arguments": arguments,
            }
        )

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if line.startswith("```"):
            if in_code_block:
                in_code_block = False
                in_shell_block = False
            else:
                in_code_block = True
                fence_info = line[3:].strip().split(maxsplit=1)
                language = fence_info[0].casefold() if fence_info else ""
                in_shell_block = language in {"bash", "sh", "shell"}
            pending = ""
            continue
        if not in_shell_block:
            continue
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.rstrip().endswith("\\"):
            if not pending:
                pending_line = line_number
            pending += line.rstrip()[:-1] + " "
            continue
        start_line = pending_line or line_number
        add_command(start_line, pending + line)
        pending = ""
        pending_line = 0

    if pending:
        raise AssertionError(f"{path}:{pending_line}: unfinished shell continuation")
    return examples


def test_runbook_vonkctl_examples_parse_in_the_installed_wheel(
    installed_vonkctl: Path, tmp_path: Path
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    runbook = repository_root / "docs/runbooks/vonkctl.md"
    examples = _runbook_examples(runbook)
    assert examples, f"no executable vonkctl examples found in {runbook}"

    python = installed_vonkctl.parent / "python"
    parser_program = """
import json
import sys
from pathlib import Path
import cluster_profiles
from cluster_profiles.cli import _parser

if not Path(cluster_profiles.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise RuntimeError("CLI parser was not imported from the installed wheel")

parser = _parser()
examples = json.load(sys.stdin)
for example in examples:
    try:
        parser.parse_args(example["arguments"])
    except SystemExit as error:
        if error.code not in (None, 0):
            raise RuntimeError(
                f"runbook line {example['line']} is not accepted: {example['source']}"
            ) from error
    except Exception as error:
        raise RuntimeError(
            f"runbook line {example['line']} is not accepted: {example['source']}"
        ) from error
print("parsed", len(examples))
"""
    # Keep the runbook parser and the package under test in the isolated wheel
    # environment. The subprocess invokes argparse only; it never calls CLI
    # dispatch or makes a Controller request.
    environment = {**os.environ, "PYTHONPATH": ""}
    parsed = subprocess.run(
        [str(python), "-c", parser_program],
        input=json.dumps(examples),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    assert parsed.stdout.rstrip().endswith(f"parsed {len(examples)}")
