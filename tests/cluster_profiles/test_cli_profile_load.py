"""Profile review consent crosses real terminal and noninteractive boundaries."""

import os
import select
import sys

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client.errors import ControlNotFound
from control.tests.consumer_outcomes import not_adopted

KEY = "11111111-1111-4111-8111-111111111111"
DIGEST = "c" * 64
APPLICATION = "33333333-3333-4333-8333-333333333333"


def test_profile_load_no_longer_accepts_a_caller_supplied_plan_digest():
    with not_adopted():
        cli._parser().parse_args(
            ("--profile", "2", "profile", "load", "--expected-plan", DIGEST)
        )
    assert (
        cli._parser()
        .parse_args(("--profile", "2", "profile", "load", "--yes"))
        .profile_number
        == 2
    )


class Client:
    request_timeout_seconds = 1.0

    def __init__(
        self,
        *,
        blocked=False,
        plan_digest=DIGEST,
        preparation_decisions=None,
        include_receipt_digest=True,
        next_action=None,
    ):
        self.next_action = next_action
        self.calls = []
        self.blocked = blocked
        self.plan_digest = plan_digest
        self.preparation_decisions = preparation_decisions or []
        self.include_receipt_digest = include_receipt_digest

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path.endswith("/preview"):
            return {
                "allowed": not self.blocked,
                "profile_name": "Reviewed idle",
                "profile_revision": 3,
                "plan_digest": self.plan_digest,
                "effects_digest": self.plan_digest,
                **(
                    {}
                    if self.next_action is None
                    else {"next_action": self.next_action}
                ),
                "scope": {"node_ids": ["Atlas"], "idle_node_ids": ["Atlas"]},
                "summary": {"starts": 0, "stops": 0},
                "assignments": [],
                "steps": [],
                "preparations": [],
                "preparation_decisions": self.preparation_decisions,
                "assessments": [],
                "admission_decisions": [],
                "effects": {"runs": [], "installations": [], "superseded": []},
                "reasons": (
                    [
                        {
                            "code": "cache_missing",
                            "detail": "Missing exact model archive " + "a" * 64,
                        }
                    ]
                    if self.blocked
                    else []
                ),
            }
        assert path.endswith("/load")
        assert isinstance(payload, dict)
        assert isinstance(payload.get("request_key"), str)
        assert "plan_digest" not in payload
        intended = {}
        if self.include_receipt_digest:
            intended["reviewed_plan_digest"] = self.plan_digest
        return {
            "id": APPLICATION,
            "request_key": payload["request_key"],
            "state": "succeeded",
            "progress": {"intended_profile": intended},
        }


def test_yes_loads_without_a_preview_round_trip():
    client = Client(include_receipt_digest=False)
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "load",
                "--yes",
                "--request-key",
                KEY,
                "--detach",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert [path for _, path, _ in client.calls] == ["/api/profile/2/load"]


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--no-input",),
        ("--json",),
    ],
)
def test_noninteractive_load_cannot_approve_an_unseen_or_invalid_plan(options):
    client = Client()
    assert (
        cli.main(("--profile", "2", "profile", "load", *options), control_client=client)
        == 2
    )
    # The plan may be read, but nothing is submitted without consent.
    assert [
        path for _method, path, _body in client.calls if path.endswith("/load")
    ] == []


def test_json_review_returns_one_blocked_review_without_load(vonkctl_json):
    client = Client(blocked=True)
    status, _ = vonkctl_json(
        "--profile", "2", "profile", "load", "--review", control_client=client
    )
    assert status == 2
    assert client.calls == [("POST", "/api/profile/2/preview", None)]


def test_interactive_blocked_preview_can_be_confirmed_and_submitted(monkeypatch):

    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            client = Client(blocked=True)
            os.write(master, b"yes\n")
            code = cli.main(
                ("--profile", "2", "profile", "load"), control_client=client
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        assert code == 0
        assert "Missing exact model archive" in transcript.decode()
        assert "[y/N]" in transcript.decode()
        assert [path for _, path, _ in client.calls] == [
            "/api/profile/2/preview",
            "/api/profile/2/load",
        ]
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("no", 2), ("yes", 0), (None, 2)],
)
def test_terminal_reviews_and_confirms_once(answer, expected, monkeypatch):

    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            os.write(master, b"\x04" if answer is None else (answer + "\n").encode())
            client = Client()
            generated_keys = []

            def request_key():
                generated_keys.append(KEY)
                return KEY

            code = cli.main(
                ("--profile", "2", "profile", "load", "--detach"),
                control_client=client,
                request_id_factory=request_key,
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        assert code == expected
        text = transcript.decode()
        assert DIGEST in text
        assert [path for _, path, _ in client.calls] == (
            ["/api/profile/2/preview"]
            + (["/api/profile/2/load"] if answer == "yes" else [])
        )
        assert generated_keys == ([KEY] if answer == "yes" else [])
    finally:
        os.close(master)
        os.close(slave)


def test_interactive_load_still_shows_current_effect_review(monkeypatch):

    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            os.write(master, b"yes\n")
            client = Client()
            code = cli.main(
                (
                    "--profile",
                    "2",
                    "profile",
                    "load",
                    "--detach",
                ),
                control_client=client,
                request_id_factory=lambda: KEY,
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        assert code == 0
        assert [path for _, path, _ in client.calls] == [
            "/api/profile/2/preview",
            "/api/profile/2/load",
        ]
        text = transcript.decode()
        assert DIGEST in text
    finally:
        os.close(master)
        os.close(slave)


def test_interactive_load_uses_latest_preview(monkeypatch):

    current_digest = "d" * 64
    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            os.write(master, b"yes\n")
            client = Client(plan_digest=current_digest)
            code = cli.main(
                (
                    "--profile",
                    "2",
                    "profile",
                    "load",
                    "--detach",
                ),
                control_client=client,
                request_id_factory=lambda: KEY,
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        assert code == 0
        assert [path for _, path, _ in client.calls] == [
            "/api/profile/2/preview",
            "/api/profile/2/load",
        ]
        text = transcript.decode()
        assert text.count("[y/N]") == 1
    finally:
        os.close(master)
        os.close(slave)


def test_changed_image_plan_can_be_loaded_without_repeating_review(monkeypatch):

    current_digest = "d" * 64
    image_digest = "sha256:" + "1" * 64
    archive_digest = "2" * 64
    preparation_decisions = [
        {
            "assignment_id": "assignment-1",
            "model": {"artifact_set_sha256": "3" * 64},
            "runtime_image": {
                "image_digest": image_digest,
                "oci_layout_sha256": archive_digest,
                "image_bytes": 4_294_967_296,
                "architecture": "linux-arm64",
                "runtime_interface": "vllm",
                "build_id": "build-replacement",
            },
        }
    ]
    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "r") as terminal_input,
            os.fdopen(os.dup(slave), "w", buffering=1) as terminal_error,
        ):
            monkeypatch.setattr(sys, "stdin", terminal_input)
            monkeypatch.setattr(sys, "stderr", terminal_error)
            os.write(master, b"yes\n")
            client = Client(
                plan_digest=current_digest,
                preparation_decisions=preparation_decisions,
            )
            code = cli.main(
                (
                    "--profile",
                    "2",
                    "profile",
                    "load",
                    "--detach",
                ),
                control_client=client,
                request_id_factory=lambda: KEY,
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        text = transcript.decode()
        assert code == 0
        assert "[y/N]" in text
        assert sum(path.endswith("/preview") for _, path, _ in client.calls) == 1
        assert sum(path.endswith("/load") for _, path, _ in client.calls) == 1
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("binding", ["request_key", "application"])
def test_unbound_load_receipt_is_only_looked_up_and_never_replayed(
    binding, vonkctl_json
):
    receipt: dict[str, object] = {
        "id": APPLICATION,
        "state": "queued",
        "request_key": (
            "44444444-4444-4444-8444-444444444444" if binding == "request_key" else KEY
        ),
        "progress": {"intended_profile": {"reviewed_plan_digest": DIGEST}},
    }
    if binding == "application":
        receipt.pop("id")

    class LostAnswerClient:
        request_timeout_seconds = 1.0

        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, **_kwargs):
            self.calls.append((method, path, payload))
            if method == "POST" and path == "/api/profile/2/preview":
                return {"allowed": True, "plan_digest": DIGEST}
            if method == "POST" and path == "/api/profile/2/load":
                return receipt
            if method == "GET" and path == f"/api/profile/2/requests/{KEY}":
                raise ControlNotFound(404, "not committed")
            raise AssertionError(f"unexpected remote effect {method} {path}")

    client = LostAnswerClient()
    status, document = vonkctl_json(
        "--profile",
        "2",
        "profile",
        "load",
        "--yes",
        control_client=client,
        request_id_factory=lambda: KEY,
    )

    assert status == 2
    assert document["request_key"] == KEY
    assert document["submission"]["acceptance"] == "unknown"
    paths = [path for _method, path, _payload in client.calls]
    assert "/api/profile/2/preview" not in paths
    assert paths.count("/api/profile/2/load") >= 1
    assert paths.count(f"/api/profile/2/requests/{KEY}") >= 1


def _load(client, *options):
    return cli.main(
        ("--profile", "2", "profile", "load", "--no-input", "--detach", *options),
        control_client=client,
    )


def _submitted(client):
    return [path for _method, path, _body in client.calls if path.endswith("/load")]


def test_a_plan_that_changes_nothing_is_not_submitted_and_is_not_a_failure():
    client = Client(next_action="nothing_to_change")
    assert _load(client) == 0
    assert _submitted(client) == []


def test_a_plan_that_stops_nothing_loads_without_a_prompt_or_yes():
    client = Client(next_action="load")
    assert _load(client) == 0
    assert _submitted(client) == ["/api/profile/2/load"]


def test_a_plan_that_stops_a_workload_needs_consent():
    client = Client(next_action="load_stopping")
    assert _load(client) == 2
    assert _submitted(client) == []
