"""The signed NAS setup helper's argument and private-input boundary."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import TextIO

from vonk_agent_protocol.installer_setup import NasInstallTemplate

from .filesystem import MAX_MEMBER_BYTES, MAX_TEMPLATE_BYTES, SetupError
from .prompts import PromptIo
from .setup import prepare


def open_private_answers(path: Path) -> TextIO:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_MEMBER_BYTES
        or before.st_mode & 0o077
    ):
        raise SetupError("answers file must be a private, singly linked regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    after = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or after.st_mode & 0o077
        or after.st_size > MAX_MEMBER_BYTES
    ):
        os.close(descriptor)
        raise SetupError("answers file changed while it was opened")
    return os.fdopen(descriptor, "r", encoding="utf-8")


def root_rerun_hint() -> str:
    url = os.environ.get("VONK_INSTALLER_URL", "")
    if url.startswith("https://") and re.fullmatch(r"[A-Za-z0-9~:/_.-]+", url):
        return f"rerun it as: curl -fsSL {url} | sudo sh"
    return "rerun the same installer command with sudo on the sh side: curl -fsSL <installer URL> | sudo sh"


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Vonk Forge NAS drag-and-drop bundle setup"
    )
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("."))
    parser.add_argument("--upgrade", action="store_true")
    hermes = parser.add_mutually_exclusive_group()
    hermes.add_argument(
        "--enable-hermes", dest="hermes_enabled", action="store_const", const=True
    )
    hermes.add_argument(
        "--disable-hermes", dest="hermes_enabled", action="store_const", const=False
    )
    parser.add_argument("--answers-file", type=Path)
    args = parser.parse_args(argv)
    descriptor = os.open(args.template, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as template:
        entry = os.fstat(template.fileno())
        if not stat.S_ISREG(entry.st_mode) or entry.st_size > MAX_TEMPLATE_BYTES:
            raise SetupError(
                "template payload is not a regular file of an acceptable size"
            )
        content = template.read(MAX_TEMPLATE_BYTES + 1)
        if len(content) > MAX_TEMPLATE_BYTES:
            raise SetupError("template payload exceeds the input byte limit")
    # Validation errors are redacted by main: a private prompt's invalid default
    # must never be echoed by Pydantic's input_value diagnostics.
    payload = NasInstallTemplate.model_validate_json(content)
    answers = open_private_answers(args.answers_file) if args.answers_file else None
    try:
        prompt = PromptIo(answers) if answers is not None else PromptIo()
        outcome = prepare(
            payload,
            args.output,
            prompt,
            upgrade=args.upgrade,
            hermes_enabled=args.hermes_enabled,
        )
    finally:
        if answers is not None:
            answers.close()
    if outcome.dropped_environment:
        print("Removed obsolete .env keys: " + ", ".join(outcome.dropped_environment))
    print(f"Bundle ready at {outcome.root}")


def main() -> None:
    from pydantic import ValidationError

    try:
        run()
    except ValidationError:
        print("vonk-nas-setup: template payload is invalid", file=sys.stderr)
        raise SystemExit(1) from None
    except PermissionError as error:
        print(
            f"vonk-nas-setup: permission denied for {error.filename or 'bundle'}; {root_rerun_hint()}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, ValueError) as error:
        print(f"vonk-nas-setup: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
