import json
import os
import stat
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest

from cluster_profiles import cli
from cluster_profiles.cli_files import PrivateOutput
from cluster_profiles.control_client import (
    MAX_CONTROL_DOCUMENT_BYTES,
    ControlClient,
    ControlForbidden,
    ControlMalformedResponse,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnavailable,
)

IDENTITY = "11111111-1111-4111-8111-111111111111"
TOKEN = "sensitive-enrollment-grant-" + "x" * 18
GRANT = {
    "id": IDENTITY,
    "purpose": "new-node",
    "token": TOKEN,
    "expires_at": "2026-09-22T22:00:00Z",
    "controller_endpoint": "https://forge.example.test",
    "enrollment_endpoint": "https://forge.example.test/agent/enroll",
    "ca_fingerprint": "a" * 64,
    "installer_url": "https://install.vonkforge.ai/dev/spark",
}


class EnrollmentClient:
    def __init__(self, destination, *, lost=False, identity=IDENTITY):
        self.identity = identity
        self.destination = destination
        self.lost = lost
        self.calls = []
        self.state = "pending"

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path == "/api/fleet/enroll":
            assert isinstance(payload, dict)
            assert self.destination.stat().st_mode & 0o777 == 0o600
            assert json.loads(self.destination.read_text())["id"] == self.identity
            assert payload["request_key"] == self.identity
            if self.lost:
                raise ControlTransportError("lost response " + TOKEN)
            return {
                "action": "enroll",
                "state": "pending",
                "display_name": "Atlas",
                "grant": {**GRANT, "id": self.identity},
            }
        if path.endswith("/revoke"):
            self.state = "revoked"
        assert path.startswith("/api/fleet/enrollments/")
        return {
            "id": self.identity,
            "state": self.state,
            "purpose": "new-node",
            "node_id": None,
            "display_name": "Atlas",
            "expires_at": GRANT["expires_at"],
            "consumed_at": None,
            "revoked_at": "2026-09-22T21:00:00Z" if self.state == "revoked" else None,
        }


class EnrollmentHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum):
        return self._body[:maximum]


def run(client, destination, *extra, identity=IDENTITY):
    return cli.main(
        ("fleet", "enroll", "Atlas", "--output", str(destination), *extra),
        control_client=client,
        request_id_factory=lambda: identity,
    )


@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_grant_is_delivered_privately_and_never_printed(tmp_path, capsys, flags):
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination)
    assert run(client, destination, *flags) == 0
    assert json.loads(destination.read_text()) == GRANT
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert str(destination) in captured.out
    assert len(client.calls) == 1


def test_existing_or_linked_output_never_issues_a_grant(tmp_path, capsys):
    destination = tmp_path / "existing.json"
    destination.write_text("keep")
    link = tmp_path / "linked.json"
    link.symlink_to(destination)
    for path in (destination, link):
        client = EnrollmentClient(path)
        assert run(client, path, "--json") == 2
        assert client.calls == []
    assert destination.read_text() == "keep"


def test_private_reservation_failure_closes_descriptor_before_issuance(
    tmp_path, monkeypatch, capsys
):
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination)
    descriptors = []

    def refuse_mode(descriptor, mode):
        descriptors.append(descriptor)
        raise OSError("private mode could not be confirmed")

    monkeypatch.setattr(os, "fchmod", refuse_mode)
    try:
        assert run(client, destination, "--json") == 2
        assert client.calls == []
        capsys.readouterr()
        assert not destination.exists()
        with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
            os.fstat(descriptors[0])
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    monkeypatch.undo()
    assert run(client, destination, "--json") == 0
    assert json.loads(destination.read_text()) == GRANT
    assert len(client.calls) == 1


def test_lost_response_reports_original_identity_without_minting_another_grant(
    tmp_path, capsys
):
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination, lost=True)
    assert run(client, destination, "--json") == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["id"] == IDENTITY
    assert result["grant_status"]["state"] == "pending"
    assert TOKEN not in captured.out + captured.err
    assert [method for method, _, _ in client.calls] == ["POST", "GET"]


@pytest.mark.parametrize("failure_point", ["file_sync", "directory_sync", "interrupt"])
def test_failed_private_delivery_reconciles_and_revokes_only_the_issued_grant(
    tmp_path, capsys, monkeypatch, failure_point
):
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination)
    original = PrivateOutput.write
    real_fsync = os.fsync
    sync_count = 0

    def fail_sync(descriptor):
        nonlocal sync_count
        sync_count += 1
        # The receipt's file and directory are synced before issuance. Fail
        # the secret's actual file/directory sync after bytes were written.
        directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if sync_count >= 3 and (
            (failure_point == "file_sync" and not directory)
            or (failure_point == "directory_sync" and directory)
        ):
            raise OSError("disk sync failed")
        real_fsync(descriptor)

    def fail_secret_write(self, document):
        if "token" in document and failure_point == "interrupt":
            raise KeyboardInterrupt()
        return original(self, document)

    monkeypatch.setattr(PrivateOutput, "write", fail_secret_write)
    monkeypatch.setattr(os, "fsync", fail_sync)
    assert run(client, destination, "--json") == (
        130 if failure_point == "interrupt" else 2
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["grant_status"]["state"] == "revoked"
    assert TOKEN not in captured.out + captured.err + destination.read_text()
    assert [method for method, _, _ in client.calls] == ["POST", "GET", "POST"]

    monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(PrivateOutput, "write", original)
    fresh_destination = tmp_path / "fresh-grant.json"
    fresh_identity = "22222222-2222-4222-8222-222222222222"
    fresh_client = EnrollmentClient(fresh_destination, identity=fresh_identity)
    assert run(fresh_client, fresh_destination, "--json", identity=fresh_identity) == 0
    assert json.loads(fresh_destination.read_text())["id"] == fresh_identity
    assert [method for method, _, _ in fresh_client.calls] == ["POST"]


@pytest.mark.parametrize("failure_point", ["file_sync", "directory_sync"])
def test_transient_private_delivery_sync_recovers_without_reissuing(
    tmp_path, capsys, monkeypatch, failure_point
):
    """Catches abandoning an issued grant after one recoverable local sync fault."""
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination)
    real_fsync = os.fsync
    sync_count = 0

    def fail_once(descriptor):
        nonlocal sync_count
        sync_count += 1
        if (failure_point, sync_count) in {("file_sync", 3), ("directory_sync", 4)}:
            raise OSError("transient disk sync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    assert run(client, destination, "--json") == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert json.loads(destination.read_text())["token"] == TOKEN
    assert [method for method, _, _ in client.calls] == ["POST"]


def test_output_path_replacement_never_receives_secret_bytes(tmp_path):
    destination = tmp_path / "grant.json"
    original = tmp_path / "original.json"
    victim = tmp_path / "victim.json"
    victim.write_text("keep")
    with pytest.raises(OSError, match="changed"), PrivateOutput(destination) as output:
        os.rename(destination, original)
        destination.symlink_to(victim)
        output.write(GRANT)
    assert victim.read_text() == "keep"
    assert TOKEN not in original.read_text()


def test_reenrollment_requires_consent_and_pins_the_resolved_node(tmp_path, capsys):
    node_id = "spk_" + "a" * 32
    destination = tmp_path / "replacement.json"

    class ReenrollmentClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path))
            if method == "GET":
                assert path == "/api/fleet/Atlas"
                return {"id": node_id, "display_name": "Atlas"}
            assert path == f"/api/fleet/{node_id}/re-enroll"
            assert destination.stat().st_mode & 0o777 == 0o600
            return {
                "action": "re-enroll",
                "state": "pending",
                "node_id": node_id,
                "grant": {**GRANT, "purpose": "re-enroll"},
            }

    client = ReenrollmentClient()
    args = ("fleet", "re-enroll", "Atlas", "--output", str(destination), "--json")
    assert (
        cli.main(args, control_client=client, request_id_factory=lambda: IDENTITY) == 2
    )
    assert not destination.exists()
    assert client.calls == [("GET", "/api/fleet/Atlas")]
    capsys.readouterr()
    assert (
        cli.main(
            (*args, "--yes"), control_client=client, request_id_factory=lambda: IDENTITY
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["node_id"] == node_id
    assert json.loads(destination.read_text())["purpose"] == "re-enroll"


def test_absent_destination_fails_before_issuance(tmp_path, capsys):
    destination = tmp_path / "grant.json"
    client = EnrollmentClient(destination)
    assert cli.main(("fleet", "enroll", "Atlas", "--json"), control_client=client) == 2
    assert client.calls == []
    assert not destination.exists()


def test_denied_enrollment_is_not_retried_or_reclassified_as_pending(tmp_path, capsys):
    destination = tmp_path / "grant.json"

    class DeniedClient(EnrollmentClient):
        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            raise ControlForbidden(403, "permission denied " + TOKEN)

    client = DeniedClient(destination)
    assert run(client, destination, "--json") == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["reconciliation"] == "issuance denied"
    assert result["cause"] == "ControlForbidden"
    assert TOKEN not in captured.out + captured.err
    assert [method for method, _, _ in client.calls] == ["POST"]


@pytest.mark.parametrize(
    ("response_kind", "status", "cause", "reconcile"),
    [
        ("malformed-422", 422, ControlMalformedResponse.__name__, True),
        ("oversized-422", 422, ControlResponseTooLarge.__name__, True),
        ("malformed-200", 200, ControlMalformedResponse.__name__, True),
        ("server-503", 503, ControlUnavailable.__name__, True),
        ("refused-422", 422, "ControlHTTPError", False),
    ],
    ids=(
        "malformed-json-refusal",
        "oversized-json-refusal",
        "malformed-success",
        "server-error",
        "owner-refusal",
    ),
)
def test_enrollment_refusal_skips_stale_lookup_but_ambiguous_response_reconciles(
    tmp_path, capsys, response_kind, status, cause, reconcile
):
    destination = tmp_path / "grant.json"
    token_path = tmp_path / "token"
    token_path.write_text("private-control-token")
    token_path.chmod(0o600)
    calls = []

    pending_status = {
        "id": IDENTITY,
        "state": "pending",
        "purpose": "new-node",
        "node_id": None,
        "display_name": "Atlas",
        "expires_at": GRANT["expires_at"],
        "consumed_at": None,
        "revoked_at": None,
    }

    def opener(request, *, timeout):
        del timeout
        method = request.get_method()
        path = request.full_url.removeprefix("https://forge.example.test")
        calls.append((method, path))
        if method == "POST":
            if response_kind == "oversized-422":
                body = b"x" * (MAX_CONTROL_DOCUMENT_BYTES + 1)
            elif response_kind == "refused-422":
                body = b'{"detail":"invalid enrollment request","issues":[]}'
            elif response_kind == "server-503":
                body = b'{"detail":"temporarily unavailable"}'
            else:
                body = b"not-json"
            if status >= 400:
                headers = Message()
                headers["Content-Type"] = "application/json"
                raise HTTPError(
                    request.full_url, status, "refused", headers, BytesIO(body)
                )
            return EnrollmentHTTPResponse(status, body)
        return EnrollmentHTTPResponse(200, json.dumps(pending_status).encode())

    client = ControlClient("https://forge.example.test", token_path, opener=opener)
    assert (
        cli.main(
            ("fleet", "enroll", "Atlas", "--output", str(destination), "--json"),
            control_client=client,
            request_id_factory=lambda: IDENTITY,
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["cause"] == cause
    if reconcile:
        assert result["grant_status"]["state"] == "pending"
        assert calls == [
            ("POST", "/api/fleet/enroll"),
            ("GET", f"/api/fleet/enrollments/{IDENTITY}"),
        ]
    else:
        assert result["reconciliation"] == "issuance refused"
        assert "grant_status" not in result
        assert calls == [("POST", "/api/fleet/enroll")]
