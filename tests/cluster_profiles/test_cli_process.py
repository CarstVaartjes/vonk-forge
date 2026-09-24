from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from io import StringIO
from pathlib import Path

import pytest

from cluster_profiles import cli
from cluster_profiles.cli_completion import completion_script
from cluster_profiles.control_client import ControlClient


class Observations:
    request_timeout_seconds = 15.0

    def __init__(self, *documents: dict[str, object]) -> None:
        self.documents = list(documents)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        return self.documents.pop(0) if len(self.documents) > 1 else self.documents[0]


def test_human_usage_failure_preserves_result_pipe(capsys) -> None:
    assert cli.main(("unknown-command",)) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "invalid choice" in captured.err


def test_inspecting_failed_work_is_a_successful_read(capsys) -> None:
    client = Observations({"id": "application-1", "state": "failed"})
    assert cli.main(("profile", "progress", "--json"), control_client=client) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "failed"


def test_blocked_preview_is_not_successful_admission(capsys) -> None:
    client = Observations({"allowed": False, "reasons": [{"code": "cache_missing"}]})
    assert (
        cli.main(
            ("--profile", "1", "profile", "load", "--dry-run", "--json"),
            control_client=client,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["allowed"] is False
    assert client.calls == [("POST", "/api/profile/1/preview")]


def test_no_command_is_offline_orientation(monkeypatch, capsys) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("orientation accessed credentials or update service")

    monkeypatch.setattr(cli.ControlClient, "from_environment", unexpected)
    monkeypatch.setattr(cli, "begin_interactive_update_check", unexpected)
    assert cli.main(()) == 0
    captured = capsys.readouterr()
    assert "fleet" in captured.out and "profile" in captured.out
    assert not captured.err


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--interval-seconds", "nan"),
        ("--interval-seconds", "inf"),
        ("--interval-seconds", "0"),
        ("--timeout-seconds", "-1"),
        ("--timeout-seconds", "301"),
    ],
)
def test_invalid_wait_options_fail_before_any_request(flag, value, capsys) -> None:
    client = Observations({"state": "succeeded"})
    assert (
        cli.main(
            ("model", "detail", "model-1", flag, value, "--json"),
            control_client=client,
        )
        == 2
    )
    assert not client.calls
    assert json.loads(capsys.readouterr().out)["error_type"] == "arguments"


def test_profile_edit_requires_explicit_selection_before_read(capsys) -> None:
    client = Observations({"revision": 1, "assignments": []})
    assert cli.main(("profile", "name", "Coding", "--json"), control_client=client) == 2
    assert "--profile" in json.loads(capsys.readouterr().out)["error"]
    assert not client.calls


def test_watch_progress_preserves_stdout_for_final_result(capsys) -> None:
    client = Observations(
        {"operation_id": "model-1", "state": "running", "phase": "copying"},
        {"operation_id": "model-1", "state": "succeeded", "phase": "ready"},
    )
    assert (
        cli.main(
            ("model", "progress", "model-1", "--follow", "--interval-seconds", "0.01"),
            control_client=client,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "copying" in captured.err
    assert "copying" not in captured.out
    assert "ready" in captured.out
    assert "\x1b" not in captured.out + captured.err


def test_broken_output_pipe_does_not_cancel_work_or_write_again(monkeypatch) -> None:
    class ClosedPipe(StringIO):
        writes = 0

        def write(self, value: str) -> int:
            self.writes += 1
            raise BrokenPipeError("consumer exited")

    output = ClosedPipe()
    monkeypatch.setattr(cli.sys, "stdout", output)
    key = "11111111-1111-4111-8111-111111111111"
    client = Observations(
        {
            "operation_id": "download-1",
            "state": "queued",
            "action": "download",
            "selector": "model-1",
            "request_key": key,
        }
    )
    assert (
        cli.main(
            ("model", "download", "model-1", "--detach", "--json"),
            control_client=client,
            request_id_factory=lambda: key,
        )
        == 141
    )
    assert output.writes == 1
    assert client.calls == [("POST", "/api/model/model-1/download")]


def test_no_input_does_not_require_terminal_or_change_json(capsys) -> None:
    client = Observations({"nodes": []})
    assert cli.main(("fleet", "--no-input", "--json"), control_client=client) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"nodes": []}
    assert not captured.err


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_completion_is_offline_and_contains_nested_commands(shell, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("completion accessed credentials or network")

    monkeypatch.setattr(cli.ControlClient, "from_environment", unexpected)
    monkeypatch.setattr(cli, "begin_interactive_update_check", unexpected)
    assert cli.main(("completion", shell)) == 0
    script = capsys.readouterr().out
    assert "vonkctl" in script and "--interval-seconds" in script
    assert "library" in script and "loginfo" in script


def test_human_terminal_text_cannot_inject_cursor_or_hyperlink_controls(capsys):
    client = Observations(
        {
            "display_name": "Atlas\x1b[2J\x1b]8;;https://bad.test\x07Link",
            "connection": {"online_state": "offline"},
            "loaded": [],
            "installed": [],
        }
    )
    assert cli.main(("fleet", "detail", "Atlas"), control_client=client) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err
    assert "\x07" not in captured.out + captured.err


def test_json_preserves_remote_strings_and_collections(capsys):
    # JSON must bypass human-display clipping for both text and collections.
    payload: dict[str, object] = {
        "name": "Atlas\nBoreal",
        "description": "x" * 2048,
        "loaded": [{"alias": f"model-{index}"} for index in range(2048)],
    }
    client = Observations(payload)
    assert cli.main(("fleet", "detail", "Atlas", "--json"), control_client=client) == 0
    actual = json.loads(capsys.readouterr().out)
    assert isinstance(actual, Mapping)
    assert actual == payload


def test_profile_follow_pins_latest_application_before_a_new_load(capsys):
    first = "33333333-3333-4333-8333-333333333333"
    second = "44444444-4444-4444-8444-444444444444"

    class ConsecutiveLoads:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path))
            if path == f"/api/profile/applications/{first}":
                return {"id": first, "state": "failed"}
            if len(self.calls) == 1:
                return {"id": first, "state": "running"}
            return {"id": second, "state": "succeeded"}

    client = ConsecutiveLoads()
    assert (
        cli.main(
            ("profile", "progress", "--follow", "--interval-seconds", "0.01", "--json"),
            control_client=client,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["id"] == first
    assert client.calls == [
        ("GET", "/api/profile/1/progress"),
        ("GET", f"/api/profile/applications/{first}"),
    ]


def test_poll_deadline_bounds_sleep_and_retains_reconnect_identity(monkeypatch, capsys):
    from cluster_profiles import controller_cli

    now = [0.0]
    monkeypatch.setattr(controller_cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        controller_cli.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    identity = "33333333-3333-4333-8333-333333333333"
    client = Observations({"id": identity, "state": "running"})
    assert (
        cli.main(
            (
                "profile",
                "progress",
                "--follow",
                "--timeout-seconds",
                "0.1",
                "--interval-seconds",
                "30",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert now[0] <= 0.1
    assert len(client.calls) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["result"]["state"] == "running"
    assert document["observation"]["status"] == "timed_out"
    assert identity in document["observation"]["reconnect_command"]
    assert "--application" in document["observation"]["reconnect_command"]


def test_completion_scripts_parse_and_follow_option_values(tmp_path):
    for shell in ("bash", "zsh"):
        script = tmp_path / f"completion.{shell}"
        script.write_text(completion_script(cli._parser(), shell))
        subprocess.run([shell, "-n", str(script)], check=True, capture_output=True)
    script = tmp_path / "completion.bash"
    with script.open("a") as output:
        output.write(
            '\nCOMP_WORDS=(vonkctl --profile 2 profile lo)\nCOMP_CWORD=4\n_vonkctl\nprintf "%s\\n" "${COMPREPLY[@]}"\n'
        )
    result = subprocess.run(
        ["bash", str(script)], check=True, capture_output=True, text=True
    )
    assert result.stdout.splitlines() == ["load"]


def test_real_entrypoint_is_offline_and_uses_clean_error_streams():
    environment = dict(os.environ)
    environment.pop("VONK_CONTROL_URL", None)
    environment.pop("VONK_CONTROL_TOKEN_FILE", None)
    entrypoint = Path(__file__).resolve().parents[2] / "bin/vonkctl"
    for arguments, expected_status in (
        ([], 0),
        (["--version"], 0),
        (["missing-command"], 2),
        (["--json", "missing-command"], 2),
    ):
        completed = subprocess.run(
            [sys.executable, str(entrypoint), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == expected_status, completed.stderr
        if expected_status == 0:
            assert completed.stdout and not completed.stderr
        elif "--json" in arguments:
            assert json.loads(completed.stdout)["error_type"] == "arguments"
            assert not completed.stderr
        else:
            assert completed.stderr and not completed.stdout


def test_real_entrypoint_handles_closed_result_pipe_without_traceback():
    entrypoint = Path(__file__).resolve().parents[2] / "bin/vonkctl"
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            [sys.executable, str(entrypoint), "completion", "bash"],
            stdout=writer,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(writer)
    assert result.returncode == 141
    assert result.stderr == ""


def test_interrupted_follow_keeps_the_remote_identity_and_does_not_cancel(capsys):
    identity = "33333333-3333-4333-8333-333333333333"

    class InterruptedObserver(Observations):
        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path))
            if len(self.calls) > 1:
                raise KeyboardInterrupt
            return {"id": identity, "state": "running"}

    client = InterruptedObserver()
    assert (
        cli.main(
            ("profile", "progress", "--follow", "--interval-seconds", "0.01", "--json"),
            control_client=client,
        )
        == 130
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert not captured.err
    assert document["observation"]["status"] == "interrupted"
    assert identity in document["observation"]["reconnect_command"]
    assert document["result"] == {"id": identity, "state": "running"}
    assert all(method == "GET" for method, _ in client.calls)


def test_watch_keeps_fleet_filters_on_every_observation(capsys):
    class FilteredObserver(Observations):
        def request(self, method, path, payload=None, **kwargs):
            assert kwargs["query"] == {
                "health": ["stale"],
                "sort": "attention",
                "warnings_only": False,
            }
            return super().request(method, path, payload, **kwargs)

    client = FilteredObserver({"state": "running"}, {"state": "succeeded"})
    assert (
        cli.main(
            (
                "fleet",
                "--health",
                "stale",
                "--watch",
                "--interval-seconds",
                "0.01",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert len(client.calls) == 2


def test_connection_check_rejects_a_fifo_without_waiting_for_a_writer(tmp_path):
    token = tmp_path / "token-fifo"
    os.mkfifo(token, 0o600)
    environment = {
        **os.environ,
        "VONK_CONTROL_URL": "https://forge.example.test",
        "VONK_CONTROL_TOKEN_FILE": str(token),
    }
    entrypoint = Path(__file__).resolve().parents[2] / "bin/vonkctl"
    result = subprocess.run(
        [sys.executable, str(entrypoint), "--check-connection", "--json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 2
    assert "regular" in json.loads(result.stdout)["error"]
    assert not result.stderr


@pytest.mark.parametrize("credential_case", ["missing", "symlink", "insecure-mode"])
def test_connection_check_rejects_unsafe_token_file_before_api_request(
    tmp_path, credential_case, monkeypatch, capsys
):
    token = tmp_path / "controller-token"
    raw_token = "private-controller-token-never-print-this"
    if credential_case == "symlink":
        target = tmp_path / "actual-token"
        target.write_text(raw_token, encoding="utf-8")
        target.chmod(0o600)
        token.symlink_to(target)
    elif credential_case == "insecure-mode":
        token.write_text(raw_token, encoding="utf-8")
        token.chmod(0o644)

    monkeypatch.setenv("VONK_CONTROL_URL", "https://forge.example.test")
    monkeypatch.setenv("VONK_CONTROL_TOKEN_FILE", str(token))
    requests: list[tuple[str, str]] = []

    def unexpected_request(self, method, path, payload=None, **kwargs):
        requests.append((method, path))
        raise AssertionError("invalid local credentials reached the Controller")

    monkeypatch.setattr(ControlClient, "request", unexpected_request)
    assert cli.main(("--check-connection", "--json")) == 2
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert "token" in document["error"].lower()
    assert raw_token not in captured.out + captured.err
    assert not captured.err
    assert not requests


@pytest.mark.parametrize(
    ("noun", "path"),
    [
        ("fleet", "/api/jobs/work-1"),
        ("model", "/api/model/operations/work-1"),
        ("recipe", "/api/recipe/operations/work-1"),
    ],
)
def test_progress_uses_its_owner_and_distinguishes_read_from_await(noun, path, capsys):
    identity: dict[str, object] = (
        {"operation_id": "work-1"} if noun == "model" else {"id": "work-1"}
    )
    if noun == "recipe":
        identity["kind"] = "recipe.image.availability.v2"
    for flags, expected in (((), 0), (("--follow",), 2)):
        client = Observations(identity | {"state": "failed"})
        assert (
            cli.main(
                (noun, "progress", "work-1", *flags, "--json"), control_client=client
            )
            == expected
        )
        assert client.calls == [("GET", path)]
        assert json.loads(capsys.readouterr().out)["state"] == "failed"


@pytest.mark.parametrize("arguments", [("--help",), ("fleet", "--help")])
def test_argparse_help_handles_a_closed_result_pipe(arguments) -> None:
    """Argparse's early SystemExit still flushes within the CLI pipe boundary."""

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from cluster_profiles.cli import main; raise SystemExit(main())",
                *arguments,
            ],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 141, result.stderr.decode()
    assert not result.stderr
