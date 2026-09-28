"""Profile review consent crosses real terminal and noninteractive boundaries."""

import json
import os
import select

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import ControlConflict, ControlNotFound

KEY = "11111111-1111-4111-8111-111111111111"
DIGEST = "c" * 64
APPLICATION = "33333333-3333-4333-8333-333333333333"


class Client:
    request_timeout_seconds = 1.0

    def __init__(
        self,
        *,
        blocked=False,
        plan_digest=DIGEST,
        preparation_decisions=None,
        include_receipt_digest=True,
    ):
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
        assert payload.get("plan_digest") == self.plan_digest
        intended = {}
        if self.include_receipt_digest:
            intended["reviewed_plan_digest"] = self.plan_digest
        return {
            "id": APPLICATION,
            "request_key": payload["request_key"],
            "state": "succeeded",
            "progress": {"intended_profile": intended},
        }


class ChangesAfterPreviewClient(Client):
    def request(self, method, path, payload=None, **kwargs):
        if path.endswith("/load") and not any(
            called_path.endswith("/load") for _, called_path, _ in self.calls
        ):
            self.calls.append((method, path, payload))
            self.plan_digest = "d" * 64
            raise ControlConflict(409, "The current plan changed")
        return super().request(method, path, payload, **kwargs)


def test_yes_applies_latest_preview_without_a_user_supplied_digest(capsys):
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
    capsys.readouterr()
    assert sum(path.endswith("/preview") for _, path, _ in client.calls) == 1
    assert sum(path.endswith("/load") for _, path, _ in client.calls) == 1


def test_changed_plan_is_refreshed_and_the_same_request_resumes(capsys):
    client = ChangesAfterPreviewClient()
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "load",
                "--expected-plan",
                DIGEST,
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
    capsys.readouterr()
    assert sum(path.endswith("/preview") for _, path, _ in client.calls) == 1
    assert sum(path.endswith("/load") for _, path, _ in client.calls) == 2


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--no-input",),
        ("--json",),
        ("--expected-plan", DIGEST),
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


def test_interactive_blocked_preview_shows_reason_without_prompt_or_load(
    monkeypatch, capsys
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
            client = Client(blocked=True)
            code = cli.main(
                ("--profile", "2", "profile", "load"), control_client=client
            )
            terminal_error.flush()
            transcript = bytearray()
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))
        output = capsys.readouterr()
        assert code == 2
        assert output.out == ""
        assert "Blocked" in transcript.decode()
        assert "Missing exact model archive" in transcript.decode()
        assert "[y/N]" not in transcript.decode()
        assert [path for _, path, _ in client.calls] == ["/api/profile/2/preview"]
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "answer,expected",
    [("no", 2), ("yes", 0), (None, 2)],
)
def test_terminal_reviews_and_confirms_once(answer, expected, monkeypatch, capsys):
    import sys

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
        assert "Ready for review" in text and DIGEST in text
        assert text.count("[y/N]") == 1
        assert [path for _, path, _ in client.calls] == (
            ["/api/profile/2/preview"]
            + (["/api/profile/2/load"] if answer == "yes" else [])
        )
        assert generated_keys == ([KEY] if answer == "yes" else [])
        result = capsys.readouterr().out
        assert "Ready for review" not in result
        if code == 0:
            assert APPLICATION in result
    finally:
        os.close(master)
        os.close(slave)


def test_interactive_supplied_digest_still_shows_current_effect_review(
    monkeypatch, capsys
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
            os.write(master, b"yes\n")
            client = Client()
            code = cli.main(
                (
                    "--profile",
                    "2",
                    "profile",
                    "load",
                    "--expected-plan",
                    DIGEST,
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
        assert "Ready for review" in text and DIGEST in text
        assert text.count("[y/N]") == 1
        assert APPLICATION in capsys.readouterr().out
    finally:
        os.close(master)
        os.close(slave)


def test_interactive_supplied_digest_does_not_block_the_latest_preview(
    monkeypatch, capsys
):
    import sys

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
                    "--expected-plan",
                    DIGEST,
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
        assert APPLICATION in capsys.readouterr().out
    finally:
        os.close(master)
        os.close(slave)


def test_changed_image_plan_can_be_loaded_without_repeating_review(monkeypatch, capsys):
    import sys

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
                    "--expected-plan",
                    DIGEST,
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
        assert APPLICATION in capsys.readouterr().out
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("binding", ["request_key", "application"])
def test_unbound_load_receipt_is_only_looked_up_and_never_replayed(binding, capsys):
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
    status = cli.main(
        (
            "--profile",
            "2",
            "profile",
            "load",
            "--expected-plan",
            DIGEST,
            "--yes",
            "--json",
        ),
        control_client=client,
        request_id_factory=lambda: KEY,
    )

    output = capsys.readouterr()
    document = json.loads(output.out)
    assert status == 2
    assert document["request_key"] == KEY
    assert document["submission"]["acceptance"] == "unknown"
    paths = [path for _method, path, _payload in client.calls]
    assert paths.count("/api/profile/2/preview") == 0
    assert paths.count("/api/profile/2/load") >= 1
    assert paths.count(f"/api/profile/2/requests/{KEY}") >= 1
