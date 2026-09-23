"""Profile review consent crosses real terminal and noninteractive boundaries."""

import json
import os
import select

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import ControlConflict

KEY = "11111111-1111-4111-8111-111111111111"
DIGEST = "c" * 64
APPLICATION = "33333333-3333-4333-8333-333333333333"


class Client:
    request_timeout_seconds = 1.0

    def __init__(self, *, blocked=False, stale=False):
        self.calls = []
        self.blocked = blocked
        self.stale = stale

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path.endswith("/preview"):
            return {
                "allowed": not self.blocked,
                "profile_name": "Reviewed idle",
                "profile_revision": 3,
                "plan_digest": DIGEST,
                "scope": {"node_ids": ["Atlas"], "idle_node_ids": ["Atlas"]},
                "summary": {"starts": 0, "stops": 0},
                "assignments": [],
                "steps": [],
                "preparations": [],
                "preparation_decisions": [],
                "assessments": [],
                "admission_decisions": [],
                "effects": {"runs": [], "installations": [], "superseded": []},
                "reasons": [],
            }
        assert path.endswith("/load")
        if self.stale:
            raise ControlConflict(409, "Fleet profile preview is stale; review again")
        assert payload == {"request_key": KEY, "expected_plan_digest": DIGEST}
        return {
            "id": APPLICATION,
            "request_key": KEY,
            "state": "succeeded",
            "progress": {"intended_profile": {"reviewed_plan_digest": DIGEST}},
        }


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--yes",),
        ("--no-input",),
        ("--json",),
        ("--expected-plan", DIGEST),
        ("--yes", "--expected-plan", "invalid"),
    ],
)
def test_noninteractive_load_cannot_approve_an_unseen_or_invalid_plan(options, capsys):
    client = Client()
    assert (
        cli.main(("--profile", "2", "profile", "load", *options), control_client=client)
        == 2
    )
    assert client.calls == []
    capsys.readouterr()


def test_json_dry_run_returns_one_blocked_review_without_load(capsys):
    client = Client(blocked=True)
    assert (
        cli.main(
            ("--profile", "2", "profile", "load", "--dry-run", "--json"),
            control_client=client,
        )
        == 2
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["allowed"] is False
    assert output.err == ""
    assert client.calls == [("POST", "/api/profile/2/preview", None)]


@pytest.mark.parametrize(
    "answer,stale,expected", [("no", False, 2), ("yes", False, 0), ("yes", True, 2)]
)
def test_terminal_reviews_and_confirms_once_without_refreshing_stale_effects(
    answer, stale, expected, monkeypatch, capsys
):
    import sys

    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            os.write(master, (answer + "\n").encode())
            client = Client(stale=stale)
            code = cli.main(
                ("--profile", "2", "profile", "load", "--detach"),
                control_client=client,
                request_id_factory=lambda: KEY,
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        assert code == expected
        text = transcript.decode()
        assert "Ready for review" in text and DIGEST in text
        assert text.count("[y/N]") == 1
        assert [path for _, path, _ in client.calls] == ["/api/profile/2/preview"] + (
            ["/api/profile/2/load"] if answer == "yes" else []
        )
        result = capsys.readouterr().out
        assert "Ready for review" not in result
        if code == 0:
            assert APPLICATION in result
    finally:
        os.close(master)
        os.close(slave)
