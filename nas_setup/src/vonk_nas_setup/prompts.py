"""TTY-only interaction, with explicit private-file unattended input."""

from __future__ import annotations

import io
import sys
import termios
from typing import TextIO

from vonk_agent_protocol.installer_setup import (
    NasInstallModes,
    NasRequiredValuePrompt,
    NasSecretPrompt,
    single_line,
    valid_required_value,
)

from .filesystem import MAX_MEMBER_BYTES, SetupError


class InputEnded(SetupError):
    def __init__(self) -> None:
        super().__init__("input ended before setup was complete")


class PromptIo:
    def __init__(
        self, reader: TextIO | None = None, writer: TextIO | None = None
    ) -> None:
        self.reader = reader
        self.remaining_input_bytes = MAX_MEMBER_BYTES
        self.writer = (
            writer
            if writer is not None
            else (io.StringIO() if reader is not None else sys.stdout)
        )

    def note(self, message: str) -> None:
        self.writer.write(message + "\n")
        self.writer.flush()

    def line(self, label: str, *, secret: bool = False) -> str:
        if self.remaining_input_bytes <= 0:
            raise InputEnded()
        if self.reader is None:
            # Echo control is mandatory. A terminal/provider failure cannot
            # fall back to echoing stdin on headless hosts.
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                if secret:
                    original = termios.tcgetattr(terminal.fileno())
                    hidden = original.copy()
                    hidden[3] &= ~termios.ECHO
                    try:
                        termios.tcsetattr(terminal.fileno(), termios.TCSAFLUSH, hidden)
                        terminal.write(label + ": ")
                        terminal.flush()
                        value = terminal.readline(self.remaining_input_bytes + 1)
                    finally:
                        termios.tcsetattr(
                            terminal.fileno(), termios.TCSAFLUSH, original
                        )
                        terminal.write("\n")
                        terminal.flush()
                else:
                    terminal.write(label + ": ")
                    terminal.flush()
                    value = terminal.readline(self.remaining_input_bytes + 1)
        else:
            self.writer.write(label + ": ")
            self.writer.flush()
            value = self.reader.readline(self.remaining_input_bytes + 1)
        self.remaining_input_bytes -= len(value.encode())
        if self.remaining_input_bytes < 0:
            raise SetupError("setup input exceeds its private-file byte limit")
        if value == "":
            raise InputEnded()
        return value.rstrip("\r\n")

    def required(self, prompt: NasRequiredValuePrompt) -> str:
        label = prompt.prompt + (
            f" [{prompt.default}]" if prompt.default is not None else ""
        )
        while self.remaining_input_bytes > 0:
            value = self.line(label)
            if not value and prompt.default is not None:
                value = prompt.default
            if valid_required_value(value, prompt.validation):
                return value
            self.note("The value is invalid.")
        raise InputEnded()

    def secret(self, prompt: NasSecretPrompt) -> str:
        label = prompt.prompt
        if prompt.optional and "optional" not in label.lower():
            label += " (optional; leave blank to skip)"
        while self.remaining_input_bytes > 0:
            value = self.line(label, secret=True)
            if single_line(value) and (value or prompt.optional):
                return value
            self.note("The value is invalid.")
        raise InputEnded()

    def install_mode(self, modes: NasInstallModes) -> bool:
        other = (
            modes.secure_remote_value
            if modes.default == modes.lab_value
            else modes.lab_value
        )
        while self.remaining_input_bytes > 0:
            answer = (
                self.line(f"{modes.prompt} [{modes.default} / {other}]").strip()
                or modes.default
            )
            if answer in (modes.lab_value, modes.secure_remote_value):
                return answer == modes.lab_value
            self.note(f"Choose {modes.lab_value} or {modes.secure_remote_value}.")
        raise InputEnded()

    def confirm(self, label: str) -> bool:
        while self.remaining_input_bytes > 0:
            value = self.line(label + " [y/N]").strip().lower()
            if value in ("y", "yes", "", "n", "no"):
                return value in ("y", "yes")
            self.note("Please answer yes or no.")
        raise InputEnded()
