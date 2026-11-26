import json

from cluster_profiles import cli
from cluster_profiles.control_client.errors import ControlNotFound
from cluster_profiles.control_client.schema import _request_contract

REQUEST_ID = "10000000-0000-4000-8000-000000000001"
DIGEST = "d" * 64
LITERAL_SECRET = "sk-live-" + "s" * 32


class GatewayClient:
    """Holds every call to the bundled OpenAPI contract, as the real client does."""

    request_timeout_seconds = 5.0

    def __init__(self, *, revision=None, problems=()):
        self.revision = revision
        self.problems = list(problems)
        self.calls = []

    def _credential(self, method, path, payload):
        if method == "GET":
            return {"bindings": []}
        if path.endswith("/preview"):
            return {
                "name": "PARTNER",
                "action": "put",
                "added": [],
                "removed": [],
                "dependents": [],
                "effect_digest": DIGEST,
            }
        return {"name": "PARTNER", "request_id": payload["request_id"], "action": "put"}

    def request(self, method, path, payload=None, **kwargs):
        _request_contract(path, method, payload)
        self.calls.append((method, path, payload))
        body = payload or {}
        if "/credentials" in path:
            return self._credential(method, path, body)
        if path.endswith("/validate"):
            return {"problems": self.problems}
        if path.endswith("/preview"):
            return {
                "alias": "partner-large",
                "action": body["action"],
                "problems": [],
                "credential_bindings": [],
                "effects": ["gateway-restart"],
                "grace_seconds": 30,
                "failed_start_dark_seconds": 1005,
                "effect_digest": DIGEST,
            }
        if path.endswith("/accept"):
            return {
                "alias": "partner-large",
                "request_id": body["request_id"],
                "action": body["action"],
                "revision": 1,
                "state": "pending",
            }
        if self.revision is None:
            raise ControlNotFound(404, "No such model")
        return {
            "alias": "partner-large",
            "credential_bindings": ["PARTNER"],
            "desired_revision": self.revision,
            "enabled": True,
            "state": "blocked",
            "operator_action": "resume",
            "start_failures": 3,
            "published": False,
        }


def _entry(tmp_path, **params):
    entry = {
        "model_name": "partner-large",
        "litellm_params": {
            "model": "openai/partner-large",
            "api_base": "https://api.partner.example/v1",
            "api_key": "os.environ/VONK_CREDENTIAL_PARTNER",
            **params,
        },
    }
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(entry))
    return path, entry


def _run(arguments, client, capsys):
    status = cli.main(
        ["--json", *arguments],
        control_client=client,
        request_id_factory=lambda: REQUEST_ID,
    )
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_put_validates_reviews_then_accepts_exactly_that_review(tmp_path, capsys):
    path, entry = _entry(tmp_path)
    client = GatewayClient(revision=2)
    status, out, _ = _run(
        ["gateway", "model", "put", "--file", str(path), "--yes"], client, capsys
    )
    assert status == 0
    assert [(method, route) for method, route, _ in client.calls] == [
        ("POST", "/api/gateway/models/validate"),
        ("GET", "/api/gateway/models/partner-large"),
        ("POST", "/api/gateway/models/partner-large/preview"),
        ("POST", "/api/gateway/models/partner-large/accept"),
    ]
    reviewed = {"action": "put", "entry": entry, "expected_revision": 2}
    assert client.calls[2][2] == reviewed
    assert client.calls[3][2] == {
        **reviewed,
        "request_id": REQUEST_ID,
        "effect_digest": DIGEST,
    }
    assert json.loads(out)["accepted"]["request_id"] == REQUEST_ID


def test_a_refused_entry_stops_before_the_review_and_never_echoes_its_secret(
    tmp_path, capsys
):
    path, _ = _entry(tmp_path, api_key=LITERAL_SECRET)
    client = GatewayClient(
        problems=[{"path": "litellm_params.api_key", "code": "literal-secret"}]
    )
    status, out, err = _run(
        ["gateway", "model", "put", "--file", str(path), "--yes"], client, capsys
    )
    assert status != 0
    assert [route for _, route, _ in client.calls] == ["/api/gateway/models/validate"]
    assert LITERAL_SECRET not in out + err


def test_enable_resumes_a_parked_model_by_its_read_revision(capsys):
    client = GatewayClient(revision=3)
    status, _, _ = _run(
        ["gateway", "model", "enable", "partner-large", "--yes"], client, capsys
    )
    assert status == 0
    assert client.calls[1][2] == {"action": "enable", "expected_revision": 3}
    assert client.calls[2][2]["effect_digest"] == DIGEST


def test_review_changes_nothing_and_an_unconfirmed_change_is_refused(capsys):
    client = GatewayClient(revision=1)
    status, _, _ = _run(
        ["gateway", "model", "remove", "partner-large", "--review"], client, capsys
    )
    assert status == 0
    assert not any(route.endswith("/accept") for _, route, _ in client.calls)
    refused, _, _ = _run(
        ["gateway", "model", "remove", "partner-large", "--no-input"], client, capsys
    )
    assert refused != 0
    assert not any(route.endswith("/accept") for _, route, _ in client.calls)


def test_credential_put_carries_destinations_and_never_a_secret(capsys):
    client = GatewayClient()
    status, _, _ = _run(
        [
            "gateway",
            "credential",
            "put",
            "PARTNER",
            "--origin",
            "https://api.partner.example",
            "--yes",
        ],
        client,
        capsys,
    )
    assert status == 0
    body = client.calls[2][2]
    assert body["destinations"] == [
        {"origin": "https://api.partner.example", "private": False}
    ]
    assert set(body) == {"action", "destinations", "request_id", "effect_digest"}
