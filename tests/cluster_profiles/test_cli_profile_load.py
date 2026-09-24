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
    ):
        self.calls = []
        self.blocked = blocked
        self.plan_digest = plan_digest
        self.preparation_decisions = preparation_decisions or []

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
        assert payload == {"request_key": KEY, "plan_digest": DIGEST}
        return {
            "id": APPLICATION,
            "request_key": KEY,
            "state": "succeeded",
            "progress": {"intended_profile": {"reviewed_plan_digest": DIGEST}},
        }


class StaleAdmissionClient:
    request_timeout_seconds = 1.0

    def __init__(self, *, review_error=None, conflict_code="profile.stale_plan"):
        self.calls = []
        self.review_error = review_error
        self.conflict_code = conflict_code

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path.endswith("/load"):
            raise ControlConflict(
                409,
                "Fleet profile preview is stale; review the profile again before loading",
                code=self.conflict_code,
            )
        assert method == "POST" and path == "/api/profile/2/preview"
        if self.review_error is not None:
            raise self.review_error
        return {
            "allowed": False,
            "profile_name": "Changed profile",
            "profile_revision": 4,
            "plan_digest": "d" * 64,
            "scope": {"node_ids": ["Atlas"], "idle_node_ids": ["Atlas"]},
            "summary": {"starts": 0, "stops": 0},
            "assignments": [],
            "steps": [],
            "preparations": [],
            "preparation_decisions": [],
            "assessments": [],
            "admission_decisions": [],
            "effects": {"runs": [], "installations": [], "superseded": []},
            "reasons": [
                {
                    "code": "cache_missing",
                    "detail": "Missing exact model archive " + "e" * 64,
                }
            ],
        }


def test_stale_admission_shows_one_current_blocked_review_without_resubmitting(
    capsys,
):
    client = StaleAdmissionClient()
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
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    output = capsys.readouterr()
    assert [path for _method, path, _payload in client.calls] == [
        "/api/profile/2/load",
        "/api/profile/2/preview",
    ]
    assert "Changed profile" in output.err
    assert "d" * 64 in output.err
    assert "Missing exact model archive" in output.err
    assert "review" in output.err.lower() and "new" in output.err.lower()
    assert "preview is stale" in output.out.lower()


def test_stale_review_read_failure_preserves_original_refusal(capsys):
    from cluster_profiles.control_client import ControlTransportError

    client = StaleAdmissionClient(
        review_error=ControlTransportError("fresh review connection failed")
    )
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
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    output = capsys.readouterr()
    assert [path for _method, path, _payload in client.calls] == [
        "/api/profile/2/load",
        "/api/profile/2/preview",
    ]
    assert "preview is stale" in output.out.lower()
    assert "fresh review connection failed" not in output.out


def test_generic_conflict_does_not_trigger_an_automatic_fresh_review(capsys):
    client = StaleAdmissionClient(conflict_code="controller.conflict")
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
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    capsys.readouterr()
    assert [path for _method, path, _payload in client.calls] == [
        "/api/profile/2/load",
    ]


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


def test_interactive_stale_supplied_digest_shows_new_review_and_refuses(
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
        assert code == 2
        assert [path for _, path, _ in client.calls] == ["/api/profile/2/preview"]
        text = transcript.decode()
        assert "Ready for review" in text and current_digest in text
        assert "review changed" in text.casefold()
        assert "[y/N]" not in text
        assert capsys.readouterr().out == ""
    finally:
        os.close(master)
        os.close(slave)


def test_stale_image_review_distinguishes_new_archive_with_same_image_digest(
    monkeypatch, capsys
):
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
        assert code == 2
        assert image_digest in text
        assert archive_digest in text
        assert "Image size:" in text and "4294967296 bytes" in text
        assert "Architecture: linux-arm64" in text
        assert "Build: build-replacement" in text
        assert current_digest in text
        assert "review changed" in text.casefold()
        assert "[y/N]" not in text
        assert [path for _, path, _ in client.calls] == ["/api/profile/2/preview"]
        assert capsys.readouterr().out == ""
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("binding", ["request_key", "reviewed_digest", "application"])
def test_unbound_load_receipt_is_only_looked_up_and_never_replayed(binding, capsys):
    receipt: dict[str, object] = {
        "id": APPLICATION,
        "state": "queued",
        "request_key": (
            "44444444-4444-4444-8444-444444444444" if binding == "request_key" else KEY
        ),
        "progress": {
            "intended_profile": {
                "reviewed_plan_digest": "d" * 64
                if binding == "reviewed_digest"
                else DIGEST
            }
        },
    }
    if binding == "application":
        receipt.pop("id")

    class LostAnswerClient:
        request_timeout_seconds = 1.0

        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, **_kwargs):
            self.calls.append((method, path, payload))
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
    assert client.calls == [
        (
            "POST",
            "/api/profile/2/load",
            {
                "request_key": KEY,
                "plan_digest": DIGEST,
            },
        ),
        ("GET", f"/api/profile/2/requests/{KEY}", None),
    ]
